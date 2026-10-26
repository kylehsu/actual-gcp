import copy
import importlib.util
import json
from pathlib import Path
import unittest

import yaml

SOURCE = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("migration", SOURCE / "migrate_dns.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def original(self):
        return {
            "bootcmd": ["mount the existing budget disk"],
            "runcmd": ["systemctl daemon-reload", "systemctl start actual.service",
                       "systemctl start caddy.service", "systemctl start cloudflare-ddns.service"],
            "write_files": [
                {"path": migration.OLD_SERVICE, "content":
                 "[Service]\nExecStart=/usr/bin/docker run --rm -e API_KEY=test-secret -e ZONE=example.com -e SUBDOMAIN=budget -e PROXIED=true oznu/cloudflare-ddns:latest\n"},
                {"path": "/etc/systemd/system/actual.service", "content": "existing Actual config"},
                {"path": "/etc/systemd/system/caddy.service", "content": "existing Caddy config"},
                {"path": "/var/lib/cloud/scripts/per-instance/fs-prepare.sh", "content": "existing disk initialization"},
            ],
        }

    def test_migration_changes_only_dns_files_and_dns_commands(self):
        original = self.original()
        updated, entries = migration.transform(yaml.safe_dump(original), SOURCE)
        parsed = yaml.safe_load(updated)
        self.assertEqual(parsed["bootcmd"], original["bootcmd"])
        old_other = [f for f in original["write_files"] if f["path"] != migration.OLD_SERVICE]
        new_other = [f for f in parsed["write_files"] if f["path"] not in migration.FILES
                     and f["path"] != migration.CONFIG_PATH]
        self.assertEqual(new_other, old_other)
        for command in ("systemctl start actual.service", "systemctl start caddy.service"):
            self.assertIn(command, parsed["runcmd"])
        credentials = next(f for f in entries if f["path"] == migration.CONFIG_PATH)
        self.assertEqual(credentials["permissions"], "0600")
        self.assertEqual(json.loads(credentials["content"])["api_token"], "test-secret")
        self.assertNotIn(migration.OLD_SERVICE, [f["path"] for f in parsed["write_files"]])

    def test_transformation_is_idempotent(self):
        once, _ = migration.transform(yaml.safe_dump(self.original()), SOURCE)
        twice, _ = migration.transform(once, SOURCE)
        self.assertEqual(yaml.safe_load(once), yaml.safe_load(twice))

    def test_delete_on_stop_is_rejected_before_any_live_change(self):
        original = self.original()
        original["write_files"][0]["content"] = original["write_files"][0]["content"].replace(
            " -e ZONE=", " -e DELETE_ON_STOP=true -e ZONE=")
        with self.assertRaises(ValueError):
            migration.transform(yaml.safe_dump(original), SOURCE)

    def test_plain_unrelated_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            migration.transform("something: unrelated", SOURCE)


if __name__ == "__main__":
    unittest.main()
