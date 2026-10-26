# actual-gcp
Actual Budget hosted on Google Cloud's always free tier

> **Note**: This is a fork of [eatonc/actual-gcp](https://github.com/eatonc/actual-gcp) modified to use **Cloudflare DNS** instead of DuckDNS.
>
> **Key improvements:**
> - ✅ Works with any Cloudflare-managed domain (not just free subdomains)
> - ✅ Cloudflare proxy support (orange cloud) - hidden server IP, DDoS protection, CDN
> - ✅ Automatic DNS updates when VM IP changes
> - ✅ Better for users already using Cloudflare for DNS

## Background
The goal of this repository is to deploy [Actual Budget][1] running on [Google Cloud's][2] [Free Tier][3], using the Compute Engine service. This setup utilizes [Terraform][4] to deploy and automatically configure the cloud infrastructure. Some manual steps may still need to be taken, but I've tried to remove as many as possible and document the rest.

Some notes about the architecture of this setup:

* The Compute Engine instance is deployed using Google's [Container Optimized OS][5] image and the applications run within this instance using [Docker][6].
* [Cloudflare][7] is used for DNS management with automatic DNS updates. Cloudflare proxy can be enabled for hidden server IP, DDoS protection, and CDN benefits.
* [Caddy][8] is used as a reverse proxy and for automatic TLS certificate management using HTTP-01 challenges (compatible with Cloudflare proxy).
* [Terraform state][9] management is being handled by [HCP Terraform (formerly Terraform Cloud)][10], though you could opt to store your state file elsewhere if you want to (and feel comfortable doing so). Doing so is outside the scope of this documentation.

**Disclaimer**: While I've attempted to ensure that all cloud infrastructure being deployed is part of GCP's free tier, you are ultimately responsible for your own cloud spend. The Terraform code sets up an adjustible monthly billing alert to help mitigate risk of unexpected cloud costs, but it is your responsibility to monitor your cloud account.

[1]: https://actualbudget.com/
[2]: https://cloud.google.com
[3]: https://cloud.google.com/free/docs/free-cloud-features?_gl=1*16i0xkv*_up*MQ..&gclid=EAIaIQobChMItuHQrcyPiQMVFUL_AR2RuhIpEAAYASAAEgKEkPD_BwE&gclsrc=aw.ds#free-tier-usage-limits
[4]: https://www.terraform.io
[5]: https://cloud.google.com/container-optimized-os/docs
[6]: https://www.docker.com
[7]: https://www.cloudflare.com/
[8]: https://caddyserver.com/
[9]: https://developer.hashicorp.com/terraform/language/state
[10]: https://app.terraform.io
[11]: https://cloud.google.com/sdk/docs/install
[12]: https://developer.hashicorp.com/terraform/install
[13]: https://cloud.google.com/architecture/best-practices-vpc-design#custom-mode
[14]: https://developer.hashicorp.com/terraform/tutorials/cloud-get-started/cloud-login
[15]: https://git-scm.com/downloads/win
[16]: https://cloud.google.com/billing/docs/resources/currency#list_of_countries_and_regions
[17]: https://actualbudget.com/docs/overview/getting-started/

## Pre-requisites
* A [Google Cloud][2] account
* A free [HCP Terraform][10] account (unless you want to host your state locally or elsewhere)
* A domain with [Cloudflare][7] DNS management (free tier)
* The [Google Cloud CLI][11] tools installed
* [Terraform][12] installed

## Instructions
1. Set up your Cloudflare DNS and API credentials:
    * Add your domain to Cloudflare (if not already done)
    * Note your **domain name** (e.g., example.com)
    * Create a **Cloudflare API Token**:
        * Go to My Profile > API Tokens
        * Click "Create Token"
        * Use the "Edit zone DNS" template
        * Ensure the token has these permissions:
            * Zone - Zone - Read
            * Zone - DNS - Edit
        * Under Zone Resources: Select "Include - Specific zone - [your domain]"
        * Copy the generated API token (you'll only see it once!)
    * **Set SSL/TLS encryption mode**:
        * Go to SSL/TLS tab in Cloudflare dashboard
        * Set encryption mode to **"Full (strict)"**
        * This is required for HTTPS to work properly with Caddy
        * "Flexible" mode will cause infinite redirect loops
2. If you haven't already, create your organization, project (unless you're using the Default project), and workspace in [HCP Terraform][10]. This repository assumes you've created your workspace using the CLI-Driven Workflow, but you can choose one of the other methods if you're comfortable adapting the instructions to accommodate it.
    * ![HCP Terraform New Workspace](./readme_resources/hcp_tf_new_workspace.png)
3. Within your workspace, navigate to Settings > General and scroll down to Execution Mode. Set it to "Local (Custom)". This will ensure that you can execute Terraform commands at the command line. By default, the workspace will be set to your Organization Default, which may already be set to Local. If your Organization Default is Local, you don't necessarily need to force the workspace to Local, but it also won't hurt to do so. Additionally, if you are deviating from these instructions and opting to choose to run Terraform within HCP Terraform and not on your local machine, you can ignore this step and select your desired Execution Mode for your custom setup.
    * ![HCP Terraform Execution Mode](./readme_resources/hcp_tf_execution_mode.png)
4. *Optional* - Run the following command to create an SSH public/private key-pair (if on Windows, you may need to [install Git][15] first):
    * `ssh-keygen`
    * **Note** - This will be used for SSH key-pair authentication when connecting directly, without the use of the Google SSH proxy. This configuration disables direct SSH access by default, though a firewall rule does get created to allow it. It is included in case short-term "break glass" / emergency access is needed.
5. Clone this repository to your machine (or create a fork and clone your fork) and open a terminal session into the repository's directory.
6. Run the following command to initialize your Google Cloud command line tools: 
    * `gcloud init`
7. Run the following command to make your user credentials available to Application Default Credentials (ADC):
    * `gcloud auth application-default login`
8. Run the following command to enable the API services necessary for Terraform to run and configure the rest of the environment:
    * `gcloud services enable cloudresourcemanager.googleapis.com`
9. If this is a new Google Cloud environment, I recommend running the following commands to delete the default networking configuration, as new configurations will be deployed via Terraform:
    * `gcloud compute firewall-rules list`
    * For each firewall rule listed, run `gcloud compute firewall-rules delete rulename`
    * `gcloud compute networks delete default`
    * If you already have resources active using the default VPC, skip this step and update the Terraform code to remove the new 'google_compute_network' and 'google_compute_subnetwork' resources, as well as modifying the network and subnetwork references in the 'google_compute_firewall' and 'google_compute_instance' resources to point to your existing default VPC.
    * Deleting the default network and using a custom network helps align with [documented best-practices regarding VPC design][13].
10. We're now ready to begin configuring our local Terraform environment. Run the following command to [authenticate with HCP Terraform][14]:
    * `terraform login`
11. Make the following updates to the following files in the repository:
    * **Configure backend for HCP Terraform**:
        * Copy the example file: `cp backend.tf.example backend.tf`
        * Edit `backend.tf` and replace `your-organization` and `your-workspace` with your HCP Terraform organization and workspace names
    * Create a file named `sensitive.auto.tfvars` and create the following variables:
        * actual_fqdn - The fully-qualified domain name you want to use for your Actual Budget server (e.g., "budget.example.com" or "actual.example.com")
        * billing_account_name = "your_billing_account_name" - This defaults to "My Billing Account", so this only needs defined if your billing account name is something else.
            * If you're not sure what your billing account name is, run the following command to list your billing accounts:
                `gcloud billing accounts list`
        * billing_alert_currency_code = "[your_currency_code][16]" - This isn't really a sensitive variable, but to simplify things, we can put it in the same ".auto.tfvars" file. This defaults to "USD", so this only needs defined if you're using a different currency.
        * billing_alert_amount = "the_amount_you_want" - This isn't really a sensitive variable, but to simply things, we can put it in the same ".auto.tfvars" file. This defaults to "5", so this only needs defined if you want to set a different billing alert threshold.
        * cloudflare_domain = "yourdomain.com" - Your domain name from Step #1 (e.g., "example.com")
        * cloudflare_api_token = "your_api_token" - API token from Step #1 (without "Authorization: Bearer" prefix)
        * cloudflare_record_name = "subdomain" - Just the subdomain part (e.g., "budget" for budget.example.com)
        * gcp_billing_project_name = The name of your GCP billing project. This may be the same as your GCP project.
        * gcp_project_name = The name of your GCP project.
        * gcp_region = The GCP region you wish your workload to run in (for example, us-central1). Keep in mind [only certain regions are eligible for the always-free Compute Engine instance][3].
        * gcp_zone = The zone within the GCP region you want to use (for example, us-central1-c).
        * public_key_path - The absolute path on your local machine to your SSH public key, for example `/Users/yourname/.ssh/id_ed25519.pub`. Set this to a key that exists on the computer running Terraform.
        * user = "your_vm_username" - The Linux username used for the VM's SSH key. It is independent of your local computer's username and Google account email.
        * vpc_subnet_cidr = "your_cidr_range" - This isn't really a sensitive variable, but to simplify things, we can put it in the same ".auto.tfvars" file. The default is "10.0.0.0/24"; choose a range that does not conflict with your other networks.
        * **Note** - The `.gitignore` file is configured to ignore any *.auto.tfvars files. Be extremely cautious with what variable values you allow to be pushed to your source control (Git) repository.
        * **Note** - You could also define these variables within HCP Terraform if you want to have your Terraform actions performed there instead of your local command line.
        * ![Example Variables](./readme_resources/variable_values.png)
12. Run the following command to initiate Terraform:
    *  `terraform init`
13. Run the following command to execute a "plan" operation, where we can inspect what Terraform operations are expected to happen when the "apply" operation happens:
    *  `terraform plan`
14. Once you've reviewed the output of the "plan" operation and are ready to deploy the infrastructure, run the following command and confirm when prompted:
    * `terraform apply`
15. *Optional* - Post-deployment validation:
    * Once the "apply" operation has completed, navigate to the Google Cloud web console and inspect the new virtual machine.
        * ![Provisioned VM](./readme_resources/gcp_post_deployment.png)
    * **Note** - You can also run the following Google Cloud command to validate the new virtual machine:
        * `gcloud compute instances list`
        * `gcloud compute instances describe containerhost01`
    * **Important** - After deployment, DNS is configured automatically:
        * The `cloudflare-dns.service` job will automatically create the DNS A record (if it doesn't exist)
        * The record will be created with Proxy status enabled (orange cloud) for DDoS protection and CDN
        * The timer checks the IP through GCP's local metadata service every five minutes. It contacts Cloudflare on startup, after an IP change, or for a daily reconciliation.
        * **Wait 2-3 minutes** for the DNS record to be created and propagate
        * Check your Cloudflare DNS dashboard to verify the record was created
        * **Verify SSL/TLS mode is set to "Full (strict)"** in Cloudflare (not "Flexible") - this was set in Step 1 but confirm it's still correct
    * Click the SSH button in the Google Cloud console. Once connected, run `docker ps -a`. Actual and Caddy should be running; DNS maintenance uses a host systemd timer:
        * ![Docker processes](./readme_resources/docker_ps.png)
        * If the Google Cloud SSH proxy isn't working, temporarily update the "container_host_network_tags" variable in `terraform.tfvars` and re-run `terraform apply` to add the "allow-ssh" tag, which will allow SSH from your local machine. I recommend removing that network tag and re-running `terraform apply` when finished to disable direct SSH access again.
    * Inspect the containers and DNS timer using:
        * `systemctl status actual`
        * `systemctl status caddy`
        * `systemctl status cloudflare-dns.timer`
    * If the containers are running, but things aren't working as-expected, use the following commands to inspect the container logs to further troubleshoot:
        * `docker logs actual_server`
        * `docker logs caddy`
        * `sudo journalctl -u cloudflare-dns.service --since yesterday`
    * Run `findmnt /mnt/disks/data` to confirm the applications' data directory is mounted from the persistent data disk. Device letters such as `/dev/sda` and `/dev/sdb` can vary:
        * ![lsblk output](./readme_resources/lsblk_output.png)
        * **Important** This part of the configuration is critical to application data persisting across virtual machine reboots.
16. Open your web browser and navigate to the fully-qualified domain name you set for the value of the "actual_fqdn" variable (e.g., ht<span>tps://</span>budget.example.com). You should see the Actual Budget login page. You're now ready to setup your budget. Follow [Actual Budget's Getting Started][17] page for next steps.
    * ![Actual Budget login](./readme_resources/actual_login_page.png)

## DNS maintenance

The VM uses an ephemeral external IPv4 address. Stopping and starting the VM can
change that address, so DNS must be reconciled after startup. Cloudflare records
do not require a periodic heartbeat to remain configured.

`scripts/cloudflare_dns.py` runs as a systemd oneshot service on startup and every
five minutes. It reads the external IP from GCP's local metadata endpoint and
compares it with the last successfully published address in
`/var/lib/cloudflare-dns/state.json`. An unchanged IP and a fresh cache produce
**zero Cloudflare API calls**. A new boot, a changed IP, missing/corrupt state, or
24 hours since the last successful reconciliation triggers a Cloudflare lookup.
The job creates or patches the single A record only when needed, keeping the
Cloudflare proxy enabled. It fails safely if multiple matching A records exist.

Transient HTTP errors use bounded retries with backoff. Failed or unconfirmed
writes do not advance the cache, so the next timer run retries. The cache is
written atomically with mode `0600`; it does not contain credentials. The token
is stored in `/etc/cloudflare-dns/config.json` with mode `0600`, and the script
does not log tokens or API response bodies. The token is also present in GCE
user-data and Terraform state, so restrict access to those existing surfaces.

COS recreates `/etc` on every boot. `locals.tf` installs the script, configuration,
service, and timer through cloud-init and starts the timer in `runcmd`; changing
only a live unit file would not persist after reboot.

To force a reconciliation or inspect the timer from the VM:

```sh
sudo /usr/bin/python3 /etc/cloudflare-dns/reconcile.py --force
systemctl list-timers cloudflare-dns.timer
sudo journalctl -u cloudflare-dns.service --since yesterday
```

### Validation

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
terraform fmt -check
terraform init -backend=false
terraform validate
```

## Versions and updates

The host OS, application containers, and Terraform provider have separate update policies:

Keep the non-sensitive VM defaults in the tracked `terraform.tfvars` rather than duplicating them in `sensitive.auto.tfvars`. An auto-variable file can override the tracked defaults and hide a later change to the selected image family.

| Component | Configuration | Update behavior |
| --- | --- | --- |
| Host OS | `cos-125-lts` in `terraform.tfvars` | Resolves the latest image in the selected COS LTS milestone when creating a boot disk. It does not jump to a different milestone or update an existing host. |
| Actual Server | `actualbudget/actual-server:latest` in `locals.tf` | A new host pulls the current image. Updating a running host requires pulling the image and restarting `actual.service`. |
| Caddy | `caddy:alpine` in `locals.tf` | A new host pulls the current Alpine image. Updating a running host requires pulling the image and restarting `caddy.service`. |
| Terraform Google provider | `versions.tf` and `.terraform.lock.hcl` | The constraint defines the supported range; the committed lock file selects the exact provider version and checksums. `terraform init -upgrade` selects a newer compatible provider for review. |

Keep the boot disk's `ignore_changes = [image]` rule. It prevents an advancing image family from replacing the VM during a routine apply; it is the intended maintenance policy, not a temporary version pin. An explicit boot-disk and VM replacement uses the selected image at creation time. See Terraform's [lifecycle reference](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle#ignore_changes).

Automatic COS updates are [disabled by default for the selected LTS family](https://cloud.google.com/container-optimized-os/docs/concepts/auto-update), and this configuration does not enable them. Plan OS maintenance for security patches and choose a supported milestone before its [support window ends](https://cloud.google.com/container-optimized-os/docs/concepts/versioning); changing `vm_image_family` alone does not upgrade the running VM.

Keep the provider lock file in Git. A provider upgrade changes Terraform's tooling, not the VM's OS or applications; review the resulting plan before applying. If a provider cannot decode existing state, use a compatible provider instead of editing state to remove attributes. See the [dependency lock file documentation](https://developer.hashicorp.com/terraform/language/files/dependency-lock).

## Infrastructure maintenance

The VPC uses a custom subnet in the configured region. The separate data disk holds Actual's data and Caddy's configuration and certificates; it is preserved during a host replacement and has Terraform destruction protection.

Use the [host OS maintenance procedure](docs/infrastructure-operations.md#updating-the-host-os) for OS updates and the [zone move procedure](docs/infrastructure-operations.md#moving-to-a-different-zone) when changing zones. Both require backups, a reviewed plan, and verification before removing recovery copies. Device letters such as `/dev/sda` and `/dev/sdb` can change; the boot configuration identifies the data disk by `/dev/disk/by-id/google-persistent-disk-1` and mounts it at `/mnt/disks/data`.

### Working from another computer

Restore the ignored `backend.tf` and `sensitive.auto.tfvars`, use the existing HCP Terraform organization and workspace, and check the local SSH public-key path before running `terraform init` and `terraform plan`. A fresh checkout of an existing deployment must connect to its existing state.

The HCP Terraform token authenticates CLI access to state; the Cloudflare token authenticates the VM's DNS updater. They are separate credentials. [`terraform login`](https://developer.hashicorp.com/terraform/cli/commands/login) saves its token in `~/.terraform.d/credentials.tfrc.json`, outside the repository. An expired HCP token requires logging in again but does not stop the running VM or DNS updater. Authenticate the Google CLI and Application Default Credentials as described in the setup instructions as well.

## Updating Actual Server

Back up your budgets before updating Actual. Floating container tags permit newer releases; they do not automatically update an already-running container.

To update Actual Server on the running VM:

1. SSH into the virtual machine.
   ![GCP Console SSH](./readme_resources/ssh_vm.png)
2. Pull the current image and restart Actual:
   ```sh
   sudo docker pull actualbudget/actual-server:latest
   sudo systemctl restart actual.service
   ```
   ![Manual update container](./readme_resources/update_container.png)
3. Check the version in Actual.
   ![Version check](./readme_resources/actual_update.png)

To update Caddy separately, run `sudo docker pull caddy:alpine` and `sudo systemctl restart caddy.service`, then verify HTTPS. Neither application update requires replacing the host OS.

If updates eventually fail with `no space left on device`, inspect the containers and images, then remove unused Docker resources:

```sh
sudo docker ps -a
sudo docker images
sudo docker system prune -af
```

`docker system prune -af` removes unused images, stopped containers, and dangling build cache.
