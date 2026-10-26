# Infrastructure operations

## Updating the host OS

Use this procedure for COS security updates within the selected milestone or when selecting a different supported LTS milestone. The [update policy](../README.md#versions-and-updates) keeps normal applies from replacing the host just because a newer boot image is available. Leave `ignore_changes = [image]` in place and request replacement explicitly.

1. Match these settings to your Terraform variables, and create a private directory for the plan and state backup:

   ```sh
   ACTUAL_PROJECT="your-gcp-project"
   ACTUAL_ZONE="us-central1-c"
   ACTUAL_REGION="us-central1"
   ACTUAL_BACKUP_PREFIX="actual-maintenance-$(date -u +%Y%m%d-%H%M%S)"
   ACTUAL_PLAN_DIR="$(mktemp -d)"
   chmod 700 "$ACTUAL_PLAN_DIR"
   umask 077
   ```

   Run Terraform commands from the repository using the existing HCP workspace. Select a supported `vm_image_family` in `terraform.tfvars` if changing milestones, and check for overrides in auto-variable files or CLI arguments. Plan files and state contain credentials; keep them outside Git.

2. Preview the replacement while the server is still running:

   ```sh
   terraform plan \
     -replace=google_compute_instance.container_host \
     -replace=google_compute_disk.container_host_boot_disk \
     -out="$ACTUAL_PLAN_DIR/os-upgrade.tfplan"
   terraform state pull > "$ACTUAL_PLAN_DIR/before-upgrade.tfstate"
   ```

   The plan must replace only the VM and boot disk, use the intended COS image, and leave the data disk, VPC, and subnet unchanged. Do not apply a plan that replaces the data disk. A new host also pulls the current Actual and Caddy container tags; this can update the applications as well as the OS.

3. Begin downtime by stopping the VM, then snapshot both disks so application data and the old container images have a consistent recovery point:

   ```sh
   gcloud compute instances stop containerhost01 \
     --project="$ACTUAL_PROJECT" --zone="$ACTUAL_ZONE"
   gcloud compute snapshots create "$ACTUAL_BACKUP_PREFIX-data" \
     --project="$ACTUAL_PROJECT" \
     --source-disk=container-host-data-disk \
     --source-disk-zone="$ACTUAL_ZONE" --storage-location="$ACTUAL_REGION"
   gcloud compute snapshots create "$ACTUAL_BACKUP_PREFIX-boot" \
     --project="$ACTUAL_PROJECT" \
     --source-disk=container-host-boot-disk \
     --source-disk-zone="$ACTUAL_ZONE" --storage-location="$ACTUAL_REGION"
   gcloud compute snapshots describe "$ACTUAL_BACKUP_PREFIX-data" \
     --project="$ACTUAL_PROJECT" --format='value(status)'
   gcloud compute snapshots describe "$ACTUAL_BACKUP_PREFIX-boot" \
     --project="$ACTUAL_PROJECT" --format='value(status)'
   ```

   Confirm the stop command completed and both snapshots report `READY` before proceeding. Snapshots are [separately billed][20].

4. Apply the saved plan:

   ```sh
   terraform apply "$ACTUAL_PLAN_DIR/os-upgrade.tfplan"
   ```

   This recreates the host in the same zone and reattaches the existing data disk. The VM's identity and ephemeral IP can change. The Cloudflare DNS timer reconciles the A record on startup.

5. From an SSH session on the new VM, verify the OS, mounted data, containers, and timer:

   ```sh
   sudo cloud-init status --long
   cat /etc/os-release
   findmnt /mnt/disks/data
   sudo docker ps
   systemctl is-active actual.service caddy.service cloudflare-dns.timer
   ```

   Check HTTPS and open your budgets to verify their contents, then run `terraform plan` locally; it should report no changes. Do not rely on a particular `/dev/sdX` letter for the data disk.

6. After verification, remove the temporary snapshots unless you deliberately want to retain them:

   ```sh
   gcloud compute snapshots delete \
     "$ACTUAL_BACKUP_PREFIX-data" "$ACTUAL_BACKUP_PREFIX-boot" \
     --project="$ACTUAL_PROJECT"
   ```

   Keep both recovery copies until validation is complete. If an application update changes its data format, rolling back its container alone might not be sufficient; the matching data snapshot is the recovery point.

## Moving to a Different Zone

Google Cloud zones periodically run out of capacity for a given machine type, which shows up at `terraform apply` time as:

```
The zone 'projects/your-project/zones/us-central1-c' does not have enough resources available to fulfill the request. Try a different zone, or try again later.
```

Retrying often works, since these stockouts are usually temporary. If a zone stays unavailable, though, you'll want to move to another one.

**Changing `gcp_zone` does not relocate an existing zonal disk.** The compute resources inherit their zone from the provider, while an existing disk can retain the zone recorded in state. A VM cannot attach a zonal disk from another zone. Move the data explicitly and review the resulting plan; `prevent_destroy` alone does not verify that the VM and disk are in the same zone.

Moving zones therefore means physically relocating the data disk and re-pointing Terraform's state at it. A few notes before you start:

* **Pick a zone in the same region** (for example `us-central1-c` to `us-central1-a`). The always-free `e2-micro` allowance is granted per *region*, and staying in-region also leaves the regional subnet untouched, so the VPC is not involved in the move.
* **Expect a few minutes of downtime.** The VM must be destroyed to detach the data disk, so there is no zero-downtime path with a single instance and a zonal disk.
* Use the [snapshot-and-recreate workflow][19] to restore the data disk in the target zone.
* Your Let's Encrypt certificate lives on the data disk, so it moves with your data and Caddy will not need to re-issue it.

Keep the repository's `pd-standard` disk type when restoring the disk, and check [free-tier limits][3] and [storage pricing][20] before retaining additional copies.

Substitute your own project ID, old zone, and new zone throughout.

1. Stop the VM and snapshot both disks so the application is not writing during the backup. Use unique snapshot names for each operation; the names below are examples. The data snapshot supplies the disk in the new zone, while the boot snapshot preserves the old OS and container images:
    * `gcloud compute instances stop containerhost01 --project=your_gcp_project_name --zone=your_old_zone`
    * `gcloud compute snapshots create container-host-data-disk-premigration --project=your_gcp_project_name --source-disk=container-host-data-disk --source-disk-zone=your_old_zone --storage-location=your_gcp_region`
    * `gcloud compute snapshots create container-host-boot-disk-premigration --project=your_gcp_project_name --source-disk=container-host-boot-disk --source-disk-zone=your_old_zone --storage-location=your_gcp_region`
    * Confirm both snapshots are `READY` before proceeding. A new host pulls the current floating application tags, which can also update Actual and Caddy.
    * Use a regional storage location and check [current snapshot pricing][20] before retaining the snapshot.
2. Destroy the instance **and the boot disk**, which detaches the data disk without touching it:
    * `terraform destroy -target=google_compute_instance.container_host -target=google_compute_disk.container_host_boot_disk`
    * The boot disk must be named explicitly so it is recreated in the target zone along with the VM. Application data and certificates stay on the separate data disk; the boot snapshot preserves the old host for recovery.
3. Recreate the data disk in the new zone from the snapshot. Re-using the same name is fine, because disk names only have to be unique within a zone, and leaving the original in place gives you a fallback:
    * `gcloud compute disks create container-host-data-disk --project=your_gcp_project_name --zone=your_new_zone --source-snapshot=container-host-data-disk-premigration --type=pd-standard`
    * Do not pass `--size`. The snapshot already carries the size, and `gcloud` enforces a client-side 10 GB minimum that will reject smaller disks the API itself accepts. Labels do not need to be reproduced either; Terraform reconciles those in place on the next apply.
4. Point Terraform's state at the disk in its new zone. `terraform state rm` only edits state and is not blocked by `prevent_destroy`:
    * `terraform state rm google_compute_disk.container_host_data_disk`
    * `terraform import google_compute_disk.container_host_data_disk projects/your_gcp_project_name/zones/your_new_zone/disks/container-host-data-disk`
5. Set `gcp_zone` to your new zone in `sensitive.auto.tfvars`.
6. Run `terraform plan` and check it carefully before applying. You should see the boot disk and instance **created**, the data disk **updated in place** (a labels-only change), and **nothing destroyed**:
    * If instead the plan wants to *replace* the data disk, stop. That means the `ignore_changes = [snapshot]` lifecycle rule on `google_compute_disk.container_host_data_disk` is missing from your copy of `compute.tf`. A disk restored from a snapshot reports that snapshot back to Terraform, and since the resource never sets `snapshot` itself — an argument that forces replacement — Terraform would otherwise plan to destroy the disk your restore just recovered. `prevent_destroy` will catch this and abort the plan, so your data is safe either way, but the migration cannot proceed until the lifecycle rule is in place.
7. Run `terraform apply`.
8. Verify before cleaning up. SSH in and confirm the data disk is mounted and the containers are healthy:
    * `findmnt /mnt/disks/data` should show the data filesystem mounted, and `docker ps` should list `actual_server` and `caddy`; `systemctl is-active cloudflare-dns.timer` should report `active`. Device letters can change between boots and OS images.
    * The VM's public IP changes with the move. The Cloudflare DNS timer reconciles the A record after startup, keeping the proxy enabled. Verify the record and timer before considering the move complete.
    * Load your Actual Budget URL and confirm your budget data is intact.
9. Once you're satisfied, delete the old disk and both snapshots unless you want to retain recovery copies:
    * `gcloud compute disks delete container-host-data-disk --project=your_gcp_project_name --zone=your_old_zone`
    * `gcloud compute snapshots delete container-host-data-disk-premigration container-host-boot-disk-premigration --project=your_gcp_project_name`
    * Don't leave the old disk around indefinitely. The always-free allowance is 30 GB-month of standard persistent disk, and holding both copies puts a default deployment (10 GB boot + 20 GB data + 20 GB data) well over it. Persistent disk is billed hourly, so the brief overlap during the migration itself is negligible.
    * Retain the snapshot if you want a recovery point. Snapshots are separately billed; consult [current snapshot pricing][20] and the [free-tier limits][3].


[3]: https://cloud.google.com/free/docs/free-cloud-features?_gl=1*16i0xkv*_up*MQ..&gclid=EAIaIQobChMItuHQrcyPiQMVFUL_AR2RuhIpEAAYASAAEgKEkPD_BwE&gclsrc=aw.ds#free-tier-usage-limits
[19]: https://cloud.google.com/compute/docs/disks/restore-snapshot
[20]: https://cloud.google.com/compute/disks-image-pricing#standard-snapshot-pricing
