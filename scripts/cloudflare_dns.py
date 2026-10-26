#!/usr/bin/env python3
"""Reconcile one proxied A record without polling Cloudflare while idle."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib import error, parse, request

METADATA_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "network-interfaces/0/access-configs/0/external-ip"
)
API_URL = "https://api.cloudflare.com/client/v4"
RECONCILE_SECONDS = 86400
LOG = logging.getLogger("cloudflare-dns")


class SyncError(Exception):
    pass


class MissingRecord(SyncError):
    pass


class Http:
    def __init__(self, sleep=time.sleep):
        # Metadata requests must never inherit an HTTP proxy configuration.
        self.opener = request.build_opener(request.ProxyHandler({}))
        self.sleep = sleep

    def call(self, url, method="GET", headers=None, data=None):
        encoded = None if data is None else json.dumps(data).encode()
        for attempt in range(3):
            req = request.Request(url, data=encoded, headers=headers or {}, method=method)
            try:
                with self.opener.open(req, timeout=8) as response:
                    return response.read()
            except error.HTTPError as exc:
                # Do not log API bodies, request headers, or credentials.
                if exc.code == 404:
                    raise MissingRecord("Cloudflare record no longer exists") from None
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == 2:
                    raise SyncError("HTTP request failed (status %s)" % exc.code) from None
                retry_after = exc.headers.get("Retry-After", "0")
                try:
                    delay = min(16, max(2 ** attempt, float(retry_after)))
                except ValueError:
                    delay = 2 ** attempt
            except (error.URLError, OSError, TimeoutError):
                if attempt == 2:
                    raise SyncError("HTTP request failed after three attempts") from None
                delay = 2 ** attempt
            # POST may have succeeded even if its response was lost. Rediscover
            # the record on the next timer tick instead of blindly creating twice.
            if method == "POST":
                raise SyncError("Creation outcome unknown; will rediscover on next run")
            self.sleep(delay)


class Cloudflare:
    def __init__(self, token, http):
        self.http = http
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "actual-gcp-dns/1",
        }

    def call(self, path, method="GET", data=None):
        raw = self.http.call(API_URL + path, method, self.headers, data)
        try:
            body = json.loads(raw)
            if body.get("success") is not True:
                raise SyncError("Cloudflare did not confirm API success")
            return body["result"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SyncError("Invalid Cloudflare API response") from None

    def find_zone(self, name):
        zones = self.call("/zones?" + parse.urlencode({"name": name}))
        if not isinstance(zones, list) or not all(isinstance(z, dict) for z in zones):
            raise SyncError("Invalid Cloudflare zone list")
        matches = [z for z in zones if z.get("name", "").lower() == name]
        if len(matches) != 1:
            raise SyncError("Expected exactly one matching Cloudflare zone")
        return matches[0]["id"]

    def find_record(self, zone_id, name):
        records = self.call("/zones/%s/dns_records?%s" % (
            parse.quote(zone_id, safe=""),
            parse.urlencode({"name": name, "type": "A", "per_page": 100}),
        ))
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise SyncError("Invalid Cloudflare record list")
        matches = [r for r in records if r.get("name", "").lower() == name
                   and r.get("type") == "A"]
        if len(matches) > 1:
            raise SyncError("Multiple matching A records; refusing to choose one")
        return matches[0] if matches else None


def read_state(path):
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, state):
    # Only confirmed results replace the cache; a crash cannot leave partial JSON.
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_name(config):
    zone = config["zone_name"].strip().lower().rstrip(".")
    record = config["record_name"].strip().lower().rstrip(".")
    if not zone or "/" in zone or "/" in record:
        raise SyncError("Invalid DNS configuration")
    if record in ("", "@"):
        return zone, zone
    if record == zone or record.endswith("." + zone):
        return zone, record
    return zone, record + "." + zone


def reconcile(config, state_path, boot_id, http=None, now=None, force=False):
    http = http or Http()
    now = time.time() if now is None else now
    zone, name = record_name(config)
    raw_ip = http.call(METADATA_URL, headers={"Metadata-Flavor": "Google"})
    try:
        address = ipaddress.IPv4Address(raw_ip.decode().strip())
        if not address.is_global:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise SyncError("Metadata did not return a public IPv4 address") from None
    ip = str(address)
    # Credentials are deliberately excluded from persistent state.
    identity = hashlib.sha256(json.dumps([zone, name, True]).encode()).hexdigest()
    state = read_state(state_path)
    last = state.get("checked_at")
    fresh = isinstance(last, (int, float)) and 0 <= now - last < RECONCILE_SECONDS
    same_identity = state.get("identity") == identity
    if (not force and same_identity and state.get("ip") == ip
            and state.get("boot_id") == boot_id and fresh):
        return "skipped"

    api = Cloudflare(config["api_token"], http)
    zone_id = state.get("zone_id") if same_identity else None
    if not zone_id:
        zone_id = api.find_zone(zone)
    root = "/zones/%s/dns_records" % parse.quote(zone_id, safe="")
    record = None
    if same_identity and state.get("record_id"):
        try:
            record = api.call(root + "/" + parse.quote(state["record_id"], safe=""))
        except MissingRecord:
            pass
    if record is None:
        try:
            record = api.find_record(zone_id, name)
        except MissingRecord:
            # The entire zone might have been recreated since the cache was saved.
            zone_id = api.find_zone(zone)
            root = "/zones/%s/dns_records" % parse.quote(zone_id, safe="")
            record = api.find_record(zone_id, name)
    desired = {"type": "A", "name": name, "content": ip, "proxied": True}
    if record is None:
        record = api.call(root, "POST", dict(desired, ttl=1))
        result = "created"
    else:
        # Never patch a cached ID that now identifies another name or type.
        if (not isinstance(record, dict) or record.get("type") != "A"
                or record.get("name", "").lower() != name):
            raise SyncError("Cached record identity mismatch; refusing to update")
        if record.get("content") == ip and record.get("proxied") is True:
            result = "matched"
        else:
            record = api.call(root + "/" + parse.quote(record["id"], safe=""),
                              "PATCH", desired)
            result = "updated"
    if (not isinstance(record, dict) or not record.get("id")
            or any(record.get(key) != value for key, value in desired.items())):
        raise SyncError("Cloudflare did not confirm the desired DNS record")
    save_state(state_path, {
        "identity": identity, "zone_id": zone_id, "record_id": record["id"],
        "ip": ip, "boot_id": boot_id, "checked_at": now,
    })
    LOG.info("DNS %s for %s", result, name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/cloudflare-dns/config.json")
    parser.add_argument("--state-dir", default="/var/lib/cloudflare-dns")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    os.umask(0o077)
    try:
        config = json.loads(Path(args.config).read_text())
        if not all(isinstance(config.get(key), str) for key in
                   ("api_token", "zone_name", "record_name")) or not config["api_token"]:
            raise SyncError("Missing DNS configuration")
        state_dir = Path(args.state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (state_dir / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            reconcile(config, state_dir / "state.json", boot_id, force=args.force)
        return 0
    except SyncError as exc:
        # SyncError messages are constructed locally and never include API bodies.
        LOG.error("%s; next timer run will retry", exc)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Unknown exception text might include user configuration or credentials.
        LOG.error("DNS reconciliation failed; next timer run will retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
