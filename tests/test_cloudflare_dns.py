import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

SPEC = importlib.util.spec_from_file_location(
    "dns", Path(__file__).resolve().parents[1] / "scripts/cloudflare_dns.py")
dns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dns)

CONFIG = {"api_token": "test-secret", "zone_name": "example.com", "record_name": "budget"}


def record(ip="35.208.17.15", proxied=True):
    return {"id": "record-id", "name": "budget.example.com", "type": "A",
            "content": ip, "proxied": proxied}


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, url, method="GET", headers=None, data=None):
        self.calls.append((url, method, headers, data))
        if not self.responses:
            raise AssertionError("Unexpected network call: " + url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return response
        return json.dumps({"success": True, "result": response}).encode()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def run_sync(self, responses, now=100000, boot="boot-1", force=False, config=None):
        http = FakeHttp(responses)
        result = dns.reconcile(config or CONFIG, self.path, boot, http, now, force)
        self.assertEqual(http.responses, [])
        return result, http.calls

    def seed(self):
        self.run_sync([b"35.208.17.15", [{"id": "zone-id", "name": "example.com"}],
                       [record()]])

    def test_idle_checks_only_local_metadata(self):
        self.seed()
        before = self.path.read_bytes()
        result, calls = self.run_sync([b"35.208.17.15"], now=100300)
        self.assertEqual(result, "skipped")
        self.assertEqual([c[0] for c in calls], [dns.METADATA_URL])
        self.assertEqual(calls[0][2], {"Metadata-Flavor": "Google"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_boot_always_reconciles_even_with_matching_cache(self):
        self.seed()
        result, calls = self.run_sync([b"35.208.17.15", record()], now=100300, boot="boot-2")
        self.assertEqual(result, "matched")
        self.assertEqual(len(calls), 2)

    def test_daily_check_repairs_remote_drift(self):
        self.seed()
        result, calls = self.run_sync([b"35.208.17.15", record("35.208.17.99"), record()],
                                     now=186400)
        self.assertEqual(result, "updated")
        self.assertEqual(calls[-1][1], "PATCH")

    def test_changed_ip_updates_immediately(self):
        self.seed()
        result, calls = self.run_sync([b"35.208.17.16", record(), record("35.208.17.16")],
                                     now=100300)
        self.assertEqual(result, "updated")
        self.assertEqual(calls[-1][3]["content"], "35.208.17.16")

    def test_proxy_is_preserved_and_repaired(self):
        self.seed()
        result, calls = self.run_sync([b"35.208.17.15", record(proxied=False), record()],
                                     force=True)
        self.assertEqual(result, "updated")
        self.assertIs(calls[-1][3]["proxied"], True)

    def test_missing_record_is_created(self):
        result, calls = self.run_sync([b"35.208.17.15",
                                      [{"id": "zone-id", "name": "example.com"}], [], record()])
        self.assertEqual(result, "created")
        self.assertEqual(calls[-1][1], "POST")

    def test_deleted_record_is_rediscovered(self):
        self.seed()
        result, _ = self.run_sync([b"35.208.17.15", dns.MissingRecord(), [record()]], force=True)
        self.assertEqual(result, "matched")

    def test_recreated_zone_invalidates_cached_zone_id(self):
        self.seed()
        result, calls = self.run_sync([
            b"35.208.17.15", dns.MissingRecord(), dns.MissingRecord(),
            [{"id": "replacement-zone", "name": "example.com"}], [record()]], force=True)
        self.assertEqual(result, "matched")
        self.assertIn("replacement-zone", calls[-1][0])

    def test_failed_update_does_not_advance_state(self):
        self.seed()
        before = self.path.read_bytes()
        http = FakeHttp([b"35.208.17.16", record(), dns.SyncError("failure")])
        with self.assertRaises(dns.SyncError):
            dns.reconcile(CONFIG, self.path, "boot-1", http, 100300)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unconfirmed_write_does_not_advance_state(self):
        self.seed()
        before = self.path.read_bytes()
        http = FakeHttp([b"35.208.17.16", record(), record()])
        with self.assertRaises(dns.SyncError):
            dns.reconcile(CONFIG, self.path, "boot-1", http, 100300)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_metadata_never_contacts_cloudflare(self):
        for value in [b"", b"127.0.0.1", b"169.254.169.254", b"not an IP", b"::1"]:
            http = FakeHttp([value])
            with self.assertRaises(dns.SyncError):
                dns.reconcile(CONFIG, self.path, "boot-1", http, 100300)
            self.assertEqual(len(http.calls), 1)

    def test_corrupt_cache_recovers_and_contains_no_credentials(self):
        self.path.write_text("not JSON")
        self.seed()
        self.assertNotIn("test-secret", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_cached_record_identity_is_checked_before_write(self):
        self.seed()
        other = dict(record(), name="unrelated.example.com")
        http = FakeHttp([b"35.208.17.16", other])
        with self.assertRaises(dns.SyncError):
            dns.reconcile(CONFIG, self.path, "boot-1", http, 100300)
        self.assertEqual(len(http.calls), 2)

    def test_duplicate_a_records_are_not_overwritten(self):
        http = FakeHttp([b"35.208.17.15", [{"id": "zone-id", "name": "example.com"}],
                         [record(), dict(record(), id="second-record")]])
        with self.assertRaises(dns.SyncError):
            dns.reconcile(CONFIG, self.path, "boot-1", http, 100300)
        self.assertFalse(self.path.exists())

    def test_configuration_change_invalidates_cached_ids(self):
        self.seed()
        changed = dict(CONFIG, zone_name="other.example", record_name="budget")
        other = dict(record(), name="budget.other.example")
        result, calls = self.run_sync([b"35.208.17.15",
                                      [{"id": "other-zone", "name": "other.example"}], [other]],
                                     config=changed)
        self.assertEqual(result, "matched")
        self.assertIn("/zones?", calls[1][0])

    def test_clock_rollback_forces_reconciliation(self):
        self.seed()
        result, _ = self.run_sync([b"35.208.17.15", record()], now=1)
        self.assertEqual(result, "matched")


class HttpTests(unittest.TestCase):
    def client(self, outcomes):
        client = dns.Http(sleep=Mock())
        client.opener = Mock()
        client.opener.open.side_effect = outcomes
        return client

    def response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"ok"
        return response

    def test_retry_backoff_for_transient_failure(self):
        client = self.client([URLError("temporary"), self.response()])
        self.assertEqual(client.call("https://api.cloudflare.com/client/v4/zones"), b"ok")
        client.sleep.assert_called_once_with(1)

    def test_auth_failure_is_not_retried_or_disclosed(self):
        client = self.client([HTTPError("https://api.cloudflare.com", 403, "test-secret", {}, None)])
        with self.assertRaises(dns.SyncError) as caught:
            client.call("https://api.cloudflare.com/client/v4/zones")
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(client.opener.open.call_count, 1)

    def test_creation_is_not_blindly_retried(self):
        client = self.client([URLError("lost response")])
        with self.assertRaises(dns.SyncError):
            client.call("https://api.cloudflare.com/client/v4/zones/z/dns_records", "POST", data={})
        self.assertEqual(client.opener.open.call_count, 1)

    def test_rate_limit_delay_is_bounded(self):
        client = self.client([HTTPError("https://api.cloudflare.com", 429, "rate limited",
                                      {"Retry-After": "9999"}, io.BytesIO()), self.response()])
        client.call("https://api.cloudflare.com/client/v4/zones")
        client.sleep.assert_called_once_with(16)


if __name__ == "__main__":
    unittest.main()
