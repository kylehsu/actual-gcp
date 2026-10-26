import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class DiskInitializationTests(unittest.TestCase):
    def initialize(self, existing_filesystem):
        source = (Path(__file__).resolve().parents[1] / "locals.tf").read_text()
        script = re.search(r"content\s*=\s*<<-EOT5\n(.*?)^\s*EOT5", source,
                           re.MULTILINE | re.DOTALL).group(1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calls = root / "calls"
            # Run the actual boot script against command stubs, never real disks.
            for command in ("blkid", "mkfs.ext4", "mkdir", "mount", "cp"):
                stub = root / command
                stub.write_text("#!/bin/sh\n"
                                "printf '%s\\n' \"${0##*/} $*\" >> \"$DISK_TEST_LOG\"\n"
                                + ("exit \"$DISK_TEST_BLKID_STATUS\"\n"
                                   if command == "blkid" else "exit 0\n"))
                stub.chmod(0o700)
            environment = dict(os.environ, PATH=str(root), DISK_TEST_LOG=str(calls),
                               DISK_TEST_BLKID_STATUS="0" if existing_filesystem else "2")
            subprocess.run(["/bin/bash", "-eu", "-c", script], env=environment,
                           check=True, capture_output=True, text=True)
            return calls.read_text().splitlines()

    def test_existing_budget_filesystem_is_never_formatted(self):
        calls = self.initialize(existing_filesystem=True)
        self.assertFalse(any(call.startswith("mkfs.ext4 ") for call in calls))
        self.assertTrue(any(call.startswith("mount ") for call in calls))

    def test_new_disk_is_formatted_before_mounting(self):
        calls = self.initialize(existing_filesystem=False)
        formats = [index for index, call in enumerate(calls) if call.startswith("mkfs.ext4 ")]
        mounts = [index for index, call in enumerate(calls) if call.startswith("mount ")]
        self.assertEqual(len(formats), 1)
        self.assertLess(formats[0], mounts[0])


if __name__ == "__main__":
    unittest.main()
