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
    * If you already have resources active using the default VPC, skip this step and update the Terraform code to remove the new 'google_compute_network' and 'google_compute_route' resources, as well as modifying the network for the 'google_compute_firewall' and 'google_compute_instance' resources.
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
        * public_key_path - The path on your local machine to the SSH public key that was generated in Step #2 (if it was named something other than the default value defined in `compute-variables.tf`)
        * user = "your_google_username" - It should be your Google username without the "@gmail.com". If you use the SSH proxy to login from the GCP console, it will log you in automatically as this user.
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
        * The `cloudflare-ddns` container will automatically create the DNS A record (if it doesn't exist)
        * The record will be created with Proxy status enabled (orange cloud) for DDoS protection and CDN
        * The container keeps the IP updated automatically if it changes
        * **Wait 2-3 minutes** for the DNS record to be created and propagate
        * Check your Cloudflare DNS dashboard to verify the record was created
        * **Verify SSL/TLS mode is set to "Full (strict)"** in Cloudflare (not "Flexible") - this was set in Step 1 but confirm it's still correct
    * Click the SSH button in the Google Cloud console. Once connected, run `docker ps -a`. We should see three containers running:
        * ![Docker processes](./readme_resources/docker_ps.png)
        * If the Google Cloud SSH proxy isn't working, temporarily update the "container_host_network_tags" variable in `terraform.tfvars` and re-run `terraform apply` to add the "allow-ssh" tag, which will allow SSH from your local machine. I recommend removing that network tag and re-running `terraform apply` when finished to disable direct SSH access again.
    * If the three containers aren't running, you can inspect their associated systemctl services using the following commands:
        * `systemctl status actual`
        * `systemctl status caddy`
        * `systemctl status cloudflare-ddns`
    * If the containers are running, but things aren't working as-expected, use the following commands to inspect the container logs to further troubleshoot:
        * `docker logs actual_server`
        * `docker logs caddy`
        * `docker logs cloudflare-ddns`
    * Run the following command to ensure the applications' "data" directory has been mounted onto the Persistent Disk (/dev/sdb):
        * ![lsblk output](./readme_resources/lsblk_output.png)
        * **Important** This part of the configuration is critical to application data persisting across virtual machine reboots.
16. Open your web browser and navigate to the fully-qualified domain name you set for the value of the "actual_fqdn" variable (e.g., ht<span>tps://</span>budget.example.com). You should see the Actual Budget login page. You're now ready to setup your budget. Follow [Actual Budget's Getting Started][17] page for next steps.
    * ![Actual Budget login](./readme_resources/actual_login_page.png)

## Updating Actual Server
There are a couple of ways you could use to try to update Actual Server to a newer version.

1. Re-run `terraform apply`. If a newer version of the Google Container Optimized OS image has been published, the virtual machine will be destroyed and re-deployed, pulling the latest version of the actualbudget/actual-server container during the re-deploy.
2. If you want to manually trigger an update, you can perform the following steps:
    * SSH into the virtual machine.
        * ![GCP Console SSH](./readme_resources/ssh_vm.png)
    * Issue the following commands:
        ```
        docker pull actualbudget/actual-server:latest
        sudo systemctl restart actual
        ```
        * ![Manual update container](./readme_resources/update_container.png)
    * Your server should now reflect the most recent version.
        * ![Version check](./readme_resources/actual_update.png)
