#!/usr/bin/env python3
"""Migrate only DNS automation on an existing VM; never apply Terraform or reboot."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
from urllib import parse, request

import yaml

FILES = {
    "/etc/cloudflare-dns/reconcile.py": ("cloudflare_dns.py", "0600"),
    "/etc/systemd/system/cloudflare-dns.service": ("cloudflare-dns.service", "0644"),
    "/etc/systemd/system/cloudflare-dns.timer": ("cloudflare-dns.timer", "0644"),
}
CONFIG_PATH = "/etc/cloudflare-dns/config.json"
OLD_SERVICE = "/etc/systemd/system/cloudflare-ddns.service"


def transform(original, source):
    config = yaml.safe_load(original)
    if not isinstance(config, dict) or not isinstance(config.get("write_files"), list):
        raise ValueError("Expected the existing cloud-config write_files")
    by_path = {entry["path"]: entry for entry in config["write_files"]}
    if CONFIG_PATH in by_path:
        credentials = json.loads(by_path[CONFIG_PATH]["content"])
    else:
        unit = by_path[OLD_SERVICE]["content"]
        command = next(line.split("=", 1)[1] for line in unit.splitlines()
                       if line.strip().startswith("ExecStart="))
        arguments = shlex.split(command)
        environment = {}
        for index, argument in enumerate(arguments[:-1]):
            if argument in ("-e", "--env"):
                key, value = arguments[index + 1].split("=", 1)
                environment[key] = value
        if environment.get("DELETE_ON_STOP", "false").lower() == "true":
            raise ValueError("Old updater deletes DNS on stop; manual migration required")
        credentials = {"api_token": environment["API_KEY"], "zone_name": environment["ZONE"],
                       "record_name": environment["SUBDOMAIN"]}
    replacements = [{"path": path, "permissions": mode, "owner": "root",
                     "content": (source / filename).read_text()}
                    for path, (filename, mode) in FILES.items()]
    replacements.append({"path": CONFIG_PATH, "permissions": "0600", "owner": "root",
                         "content": json.dumps(credentials)})
    managed = set(FILES) | {CONFIG_PATH, OLD_SERVICE}
    config["write_files"] = [f for f in config["write_files"] if f["path"] not in managed]
    config["write_files"].extend(replacements)
    # COS regenerates /etc every boot. Persist the timer in cloud-init as well
    # as installing it live. Leave all Actual/Caddy/disk setup entries intact.
    commands = config.get("runcmd", [])
    config["runcmd"] = [command for command in commands
                        if "cloudflare-ddns" not in str(command)
                        and "cloudflare-dns" not in str(command)]
    if "systemctl daemon-reload" not in config["runcmd"]:
        config["runcmd"].insert(0, "systemctl daemon-reload")
    config["runcmd"].extend([
        "systemctl stop cloudflare-ddns.service 2>/dev/null || true",
        "systemctl start cloudflare-dns.timer",
    ])
    return "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False), replacements


def archive_files(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in entries:
            content = entry["content"].encode()
            member = tarfile.TarInfo(entry["path"].lstrip("/"))
            member.size = len(content)
            member.mode = int(entry["permissions"], 8)
            member.uid = member.gid = 0
            archive.addfile(member, io.BytesIO(content))
    return base64.b64encode(buffer.getvalue()).decode()


def cli(arguments, capture=False, stdin=None):
    # Never emit metadata, credentials, or the payload on command failure.
    result = subprocess.run(arguments, input=stdin, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError("gcloud %s failed (exit %s); command payload and stderr suppressed" % (
            " ".join(arguments[1:3]), result.returncode))
    return result.stdout if capture else ""


def describe(instance_name, common):
    return json.loads(cli(["gcloud", "compute", "instances", "describe", instance_name,
                           *common, "--format=json"], capture=True))


def set_user_data(snapshot, user_data, args, common):
    metadata = snapshot["metadata"]
    items = [dict(item) for item in metadata["items"]]
    for item in items:
        if item["key"] == "user-data":
            item["value"] = user_data
            break
    token = cli(["gcloud", "auth", "print-access-token"], capture=True).strip()
    url = "https://compute.googleapis.com/compute/v1/projects/%s/zones/%s/instances/%s/setMetadata" % (
        parse.quote(args.project, safe=""), parse.quote(args.zone, safe=""),
        parse.quote(args.instance, safe=""))
    body = json.dumps({"fingerprint": metadata["fingerprint"], "items": items}).encode()
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as response:
        operation = json.load(response)
    cli(["gcloud", "compute", "operations", "wait", operation["name"], *common, "--quiet"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--apply", action="store_true", help="Install the reviewed DNS-only change")
    args = parser.parse_args()
    os.umask(0o077)
    backup = Path(args.backup_dir).resolve()
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    common = ["--project=" + args.project, "--zone=" + args.zone]
    instance = describe(args.instance, common)
    if instance.get("status") != "RUNNING":
        raise RuntimeError("VM must already be running; this migration never starts or reboots it")
    expiration = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ssh = ["gcloud", "compute", "ssh", args.instance, *common, "--tunnel-through-iap",
           "--ssh-key-expiration=" + expiration, "--quiet"]
    if args.apply:
        preflight = "sudo /usr/bin/python3 -c 'import fcntl, ipaddress, json, urllib.request; print(\"Python runtime ready\")' && sudo systemctl is-active actual caddy"
        cli([*ssh, "--command=" + preflight])
        # First SSH access may add a temporary key to instance metadata. Capture
        # the metadata fingerprint only after that routine sign-in is complete.
        current = describe(args.instance, common)
        if current["id"] != instance["id"]:
            raise RuntimeError("VM identity changed during preflight")
        instance = current
    original = next(item["value"] for item in instance.get("metadata", {}).get("items", [])
                    if item["key"] == "user-data")
    updated, entries = transform(original, Path(__file__).resolve().parent)
    (backup / "user-data.before.yaml").write_text(original)
    (backup / "user-data.after.yaml").write_text(updated)
    (backup / "instance-id.txt").write_text(str(instance["id"]) + "\n")
    print("Prepared DNS-only migration; protected backup directory: " + str(backup))
    print("Unchanged: VM identity, disks, Actual, Caddy, networking, and DNS proxy mode.")
    if not args.apply:
        print("Review the change, then rerun with a new backup directory and --apply.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_backup = "/var/lib/cloudflare-dns-migration/" + stamp
    payload = archive_files(entries)
    # Payload consists solely of the four known DNS files. Reconcile successfully
    # before retiring the old updater; failures leave the old service available.
    install = "\n".join([
        "set -eu", "umask 077", "install -d -m 700 " + shlex.quote(remote_backup),
        "if test -f " + OLD_SERVICE + "; then cp -p " + OLD_SERVICE + " " +
        shlex.quote(remote_backup + "/cloudflare-ddns.service") + "; fi",
        "printf '%s' " + shlex.quote(payload) + " | base64 -d | tar -xzf - -C /",
        "chmod 700 /etc/cloudflare-dns",
        "/usr/bin/python3 -m py_compile /etc/cloudflare-dns/reconcile.py",
        "systemd-analyze verify /etc/systemd/system/cloudflare-dns.service /etc/systemd/system/cloudflare-dns.timer",
        "systemctl daemon-reload",
        "systemctl start cloudflare-dns.service",
    ])
    # Send the credential-bearing archive on stdin, never in process arguments.
    cli([*ssh, "--command=sudo bash -s"], stdin=install)
    # Use optimistic concurrency so another metadata edit is never overwritten.
    current = describe(args.instance, common)
    before = instance.get("metadata", {}).get("fingerprint")
    if current["id"] != instance["id"] or current.get("metadata", {}).get("fingerprint") != before:
        raise RuntimeError("Metadata changed concurrently; old updater remains active")
    set_user_data(instance, updated, args, common)
    verified = describe(args.instance, common)
    actual = next(item["value"] for item in verified["metadata"]["items"] if item["key"] == "user-data")
    if actual != updated or verified["id"] != instance["id"]:
        raise RuntimeError("Boot configuration verification failed; protected backups are available")
    activate = "\n".join([
        "set -eu", "systemctl start cloudflare-dns.timer",
        "systemctl stop cloudflare-ddns.service 2>/dev/null || true",
        "if systemctl is-active --quiet cloudflare-ddns.service; then exit 1; fi",
        "if docker ps --format '{{.Names}}' | grep -qx cloudflare-ddns; then exit 1; fi",
        "systemctl is-active cloudflare-dns.timer actual caddy",
    ])
    try:
        cli([*ssh, "--command=sudo bash -c " + shlex.quote(activate)])
    except RuntimeError:
        cli([*ssh, "--command=sudo systemctl stop cloudflare-dns.timer; sudo systemctl start cloudflare-ddns.service"])
        current = describe(args.instance, common)
        if current["metadata"]["fingerprint"] == verified["metadata"]["fingerprint"]:
            set_user_data(current, original, args, common)
        raise
    smoke = "\n".join([
        "import importlib.util, json, pathlib, time",
        "spec = importlib.util.spec_from_file_location('dns_sync', '/etc/cloudflare-dns/reconcile.py')",
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)",
        "config = json.loads(pathlib.Path('/etc/cloudflare-dns/config.json').read_text())",
        "http = mod.Http(); original_call = http.call; calls = []",
        "def tracked(url, *args, **kwargs):",
        "    calls.append(url); return original_call(url, *args, **kwargs)",
        "http.call = tracked",
        "boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()",
        "result = mod.reconcile(config, pathlib.Path('/var/lib/cloudflare-dns/state.json'), boot, http)",
        "assert result == 'skipped' and calls == [mod.METADATA_URL]",
        "print('Local-only repeat check verified: zero Cloudflare API calls')",
    ])
    cli([*ssh, "--command=sudo /usr/bin/python3 -c " + shlex.quote(smoke)])
    cli([*ssh, "--command=sudo rm -f /etc/systemd/system/cloudflare-ddns.service && sudo systemctl daemon-reload"])
    print("Verified: DNS reconciled, local-only repeat check, timer active, boot configuration persisted.")
    print("No VM restart or disk changes were performed. Remote service backup: " + remote_backup)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc))
        print("Migration stopped; check the protected backups and VM status.")
        raise SystemExit(1)
    except Exception:
        print("Migration stopped. Credentials and command payloads are suppressed. Check the protected backups and VM status.")
        raise SystemExit(1)
