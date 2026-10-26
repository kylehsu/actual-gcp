# Infrastructure operations

## Custom-mode VPC adoption

Earlier versions of this repository created an auto-mode VPC network (Google Cloud's default), which conflicts with the [documented best-practices regarding VPC design][13] this repository otherwise follows. The network configuration has since been changed to create a custom-mode VPC instead.

**If you deployed before this change, do not just run `terraform apply`.** Google Cloud does not support converting `auto_create_subnetworks` in place through the Terraform provider, so Terraform will plan to destroy and recreate your VPC network — an operation Google Cloud will reject outright, since your firewall rules and running instance still reference it. Instead, perform this one-time migration first:

1. Convert your existing network to custom mode directly (this is an in-place, non-Terraform operation and does not affect your running instance):
    * `gcloud compute networks update vpc-network --switch-to-custom-subnet-mode`
2. Find the CIDR range of your existing auto-created subnet in your deployment region:
    * `gcloud compute networks subnets describe vpc-network --region=your_gcp_region --format="value(ipCidrRange)"`
3. Set the `vpc_subnet_cidr` variable in your `sensitive.auto.tfvars` (or `terraform.tfvars`) to the value returned above, so the subnet Terraform now manages matches what already exists.
4. Import your existing subnet into Terraform's state so it's adopted rather than recreated:
    * `terraform import google_compute_subnetwork.vpc_subnet projects/your_gcp_project_name/regions/your_gcp_region/subnetworks/vpc-network`
5. Run `terraform plan`. It should report no changes (or only benign ones). If it still wants to replace your network, subnet, or instance, stop and re-check steps 2-4 before applying.

## Moving to a Different Zone

Google Cloud zones periodically run out of capacity for a given machine type, which shows up at `terraform apply` time as:

```
The zone 'projects/your-project/zones/us-central1-c' does not have enough resources available to fulfill the request. Try a different zone, or try again later.
```

Retrying often works, since these stockouts are usually temporary. If a zone stays unavailable, though, you'll want to move to another one.

**Do not simply change `gcp_zone` and run `terraform apply`.** No resource in this repository sets `zone` explicitly — they all inherit it from the provider — and on `google_compute_disk` the `zone` attribute is Optional+Computed. That means Terraform keeps the zone already recorded in state and reports *no change at all* for your disks. Your instance moves to the new zone, your data disk does not, and the apply destroys the running VM and then fails creating the replacement, because an instance cannot attach a disk from another zone. You are left with the server down and the disks split across two zones. `prevent_destroy` does not protect you here, because nothing in that plan proposes destroying the data disk.

Moving zones therefore means physically relocating the data disk and re-pointing Terraform's state at it. A few notes before you start:

* **Pick a zone in the same region** (for example `us-central1-c` to `us-central1-a`). The always-free `e2-micro` allowance is granted per *region*, and staying in-region also leaves the regional subnet untouched, so the VPC is not involved in the move.
* **Expect a few minutes of downtime.** The VM must be destroyed to detach the data disk, so there is no zero-downtime path with a single instance and a zonal disk.
* `gcloud compute disks move` looks like a shortcut, and the command is still present and still documented in the [gcloud reference][18]. The backend `moveDisk` API it calls, however, now rejects the request with `HTTPError 400: The moveDisk API is scheduled for deprecation.` That message states the restriction applies to projects created on or after September 29, 2025, but it has been observed on a project created in October 2024, so don't count on the command working regardless of your project's age. Google's [own guidance for changing a disk's zone][19] is the snapshot-and-recreate workflow used below.
* Your Let's Encrypt certificate lives on the data disk, so it moves with your data and Caddy will not need to re-issue it.

### A note on Hyperdisk

The `moveDisk` deprecation message points at Google's "migrate to Hyperdisk" guide, which makes it look like this repository ought to move off Persistent Disk. It should not, for two independent reasons:

* **`e2-micro` cannot attach a Hyperdisk.** Attempting it fails at instance creation with `hyperdisk-balanced disk type cannot be used by e2-micro machine type`. The same rejection occurs for `e2-small`, `e2-medium`, and `e2-standard-2`, so this is not just a shared-core limitation. Hyperdisk would mean leaving the E2 family altogether, and `e2-micro` is the only machine type the free tier covers.
* **The free tier covers standard Persistent Disk only.** The [Always Free limits][3] read "30 GB-months standard persistent disk" — not balanced PD, and not Hyperdisk. Any Hyperdisk volume is billed from the first byte.

Between the two, adopting Hyperdisk would forfeit the always-free VM *and* the always-free disk, which is the entire premise of this repository. `pd-standard` remains the correct choice here, and the snapshot-and-recreate procedure below is how you move it between zones.

Substitute your own project ID, old zone, and new zone throughout.

1. Snapshot the data disk. This is both the migration vehicle and your rollback point:
    * `gcloud compute snapshots create container-host-data-disk-premigration --source-disk=container-host-data-disk --source-disk-zone=your_old_zone --storage-location=your_gcp_region`
    * Use a regional storage location and check [current snapshot pricing][20] before retaining the snapshot.
2. Destroy the instance **and the boot disk**, which detaches the data disk without touching it:
    * `terraform destroy -target=google_compute_instance.container_host -target=google_compute_disk.container_host_boot_disk`
    * The boot disk must be named explicitly. It has the same Optional+Computed `zone` behavior as the data disk, so if you leave it in place it stays in the old zone and the new instance will fail to launch against it. It holds no state of its own, so destroying it costs you nothing.
3. Recreate the data disk in the new zone from the snapshot. Re-using the same name is fine, because disk names only have to be unique within a zone, and leaving the original in place gives you a fallback:
    * `gcloud compute disks create container-host-data-disk --zone=your_new_zone --source-snapshot=container-host-data-disk-premigration --type=pd-standard`
    * Do not pass `--size`. The snapshot already carries the size, and `gcloud` enforces a client-side 10 GB minimum that will reject smaller disks the API itself accepts. Labels do not need to be reproduced either; Terraform reconciles those in place on the next apply.
4. Point Terraform's state at the disk in its new zone. `terraform state rm` only edits state and is not blocked by `prevent_destroy`:
    * `terraform state rm google_compute_disk.container_host_data_disk`
    * `terraform import google_compute_disk.container_host_data_disk projects/your_gcp_project_name/zones/your_new_zone/disks/container-host-data-disk`
5. Set `gcp_zone` to your new zone in `sensitive.auto.tfvars`.
6. Run `terraform plan` and check it carefully before applying. You should see the boot disk and instance **created**, the data disk **updated in place** (a labels-only change), and **nothing destroyed**:
    * If instead the plan wants to *replace* the data disk, stop. That means the `ignore_changes = [snapshot]` lifecycle rule on `google_compute_disk.container_host_data_disk` is missing from your copy of `compute.tf`. A disk restored from a snapshot reports that snapshot back to Terraform, and since the resource never sets `snapshot` itself — an argument that forces replacement — Terraform would otherwise plan to destroy the disk your restore just recovered. `prevent_destroy` will catch this and abort the plan, so your data is safe either way, but the migration cannot proceed until the lifecycle rule is in place.
7. Run `terraform apply`.
8. Verify before cleaning up. SSH in and confirm the data disk is mounted and the containers are healthy:
    * `lsblk` should show `sdb` mounted at `/mnt/disks/data`, and `docker ps` should list `actual_server` and `caddy`; `systemctl is-active cloudflare-dns.timer` should report `active`.
    * The VM's public IP changes with the move. The Cloudflare DNS timer reconciles the A record after startup, keeping the proxy enabled. Verify the record and timer before considering the move complete.
    * Load your Actual Budget URL and confirm your budget data is intact.
9. Once you're satisfied, delete the old disk, and the snapshot if you no longer want it:
    * `gcloud compute disks delete container-host-data-disk --zone=your_old_zone`
    * `gcloud compute snapshots delete container-host-data-disk-premigration`
    * Don't leave the old disk around indefinitely. The always-free allowance is 30 GB-month of standard persistent disk, and holding both copies puts a default deployment (10 GB boot + 20 GB data + 20 GB data) well over it. Persistent disk is billed hourly, so the brief overlap during the migration itself is negligible.
    * Retain the snapshot if you want a recovery point. Snapshots are separately billed; consult [current snapshot pricing][20] and the [free-tier limits][3].


[3]: https://cloud.google.com/free/docs/free-cloud-features?_gl=1*16i0xkv*_up*MQ..&gclid=EAIaIQobChMItuHQrcyPiQMVFUL_AR2RuhIpEAAYASAAEgKEkPD_BwE&gclsrc=aw.ds#free-tier-usage-limits
[13]: https://cloud.google.com/architecture/best-practices-vpc-design#custom-mode
[18]: https://cloud.google.com/sdk/gcloud/reference/compute/disks/move
[19]: https://cloud.google.com/compute/docs/disks/migrate-to-hyperdisk
[20]: https://cloud.google.com/compute/disks-image-pricing#standard-snapshot-pricing
