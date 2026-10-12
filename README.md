# PingDirectory EKS Spot development project

A Codex-ready workspace for Terraform, Ping DevOps server profiles, and live blue/green LDAPS development across three AWS Availability Zones.

The packaged default runs directory and proxy workloads on **Spot**. Each directory replica has its own retained EBS volume. This is a runnable development project with local validation; it is **not a tested production RPO=0 deployment**. Cross-AZ write acknowledgment, live profile/bootstrap validation, and failure testing remain explicit gates in [docs/ROADMAP.md](docs/ROADMAP.md).

## Included layout

| Path | Purpose |
| --- | --- |
| `terraform/` | VPC, per-AZ networking and Spot groups, EKS, access, Pod Identity, EBS driver, ECR |
| `profiles/pingdirectory/` | Editable server profile, base DN, assurance example and AZ setup hook |
| `profiles/pingdirectoryproxy/` | Trusted TLS backends and explicit blue/green routing |
| `images/` | Bake profiles into versioned Ping images; no Git credentials needed in pods |
| `scripts/project.py` | Render/deploy, secrets, image builds, status, gated promotion |
| `.devcontainer/` | Linux x86-64 development tools for a remote VS Code/Docker host |
| `AGENTS.md` | Project instructions for Codex |
| `tests/` | Promotion boundary tests |
| `docs/` | Architecture, cutover/recovery runbook and production acceptance work |

Baseline: Ping chart **0.16.0**, DevOps images **2609**, PingDirectory/Proxy **11.1.0.1**. Terraform **1.13.5**, Helm **3.19.0**, EKS/Kubectl **1.35**. The chart is vendored with its official SHA-256 checked before rendering. The AWS provider is recorded in `terraform/.terraform.lock.hcl` after validation. Keep version changes deliberate; `latest` is not an image tag in the project.

## Remote computer setup

Assumption: a Linux x86-64 remote computer with Docker, or VS Code Remote SSH and Dev Containers. The infrastructure still runs in your AWS account; the remote computer is the development workstation. ARM hosts need adjusted tool downloads/emulation.

1. Unzip the project and open its directory with your existing Codex setup. `AGENTS.md` supplies repository guidance.
2. For VS Code, open the remote directory and choose **Reopen in Container**. Create `~/.aws` first because it is mounted for your AWS CLI profile. The Docker feature uses the remote host's daemon.
3. Alternatively install Python 3, Terraform, Helm, AWS CLI v2, kubectl, Docker, OpenSSL, Java keytool and make directly. Use a Python virtual environment, then run:

```bash
python3 -m venv .venv
. .venv/bin/activate
make setup
make validate
```

No AWS credentials or Ping licenses are included. Authenticate with your existing AWS CLI profile, for example AWS SSO. Set `AWS_PROFILE` for all Terraform and AWS CLI commands.

### Windows PowerShell setup

Install Python 3 with the Windows Python launcher, make, Git for Windows (for profile shell syntax checks), and Helm (for chart rendering), then open a new PowerShell session. Helm must be on PATH; validation finds Git's shell in standard Windows installation locations when `sh` is not on PATH. If `py -3 --version` reports no installed Python, install Python before continuing. Create a virtual environment and explicitly select its interpreter for make:

```powershell
py -3 -m venv .venv
make setup PYTHON=.venv/Scripts/python.exe
make validate PYTHON=.venv/Scripts/python.exe
```

The Makefile defaults to `py -3` on Windows and `python3` elsewhere; `PYTHON` can override either default. Setup preserves an existing `config.json`. The Linux dev container remains the documented environment for the full deployment toolchain.

## Configure and provision

Edit `config.json` and copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars`. Set your real IAM role/user ARN and remote host's public egress CIDR. Defaults use **ap-southeast-2** and three AZs; edit both configuration files when changing region/cluster. Review the Ping EULA before setting `accept_eula=true`.

```bash
make terraform-init
make terraform-check
make plan
# Review the saved AWS plan before applying it.
make apply
make kubeconfig
```

`make apply` creates paid AWS infrastructure. Defaults provision two xlarge Spot nodes per AZ and three NAT gateways. There is no automatic apply in CI. Group maximums are bounds, not an installed node autoscaler. The API endpoint accepts only your configured CIDRs. Worker nodes are private, with no SSH ingress configured.

Use a durable encrypted remote Terraform backend before sharing state with other engineers. Infrastructure Terraform is intentionally separate from Kubernetes/Helm deployment to avoid provider bootstrap dependencies.

## Supply license and development secrets

Supply your two authorized product license files: `.secrets/PingDirectory.lic` and `.secrets/PingDirectoryProxy.lic`. Keep them out of Git. This starter mounts existing licenses; automatic download using DevOps credentials is not implemented.

```bash
make secrets
```

This generates passwords and a trusted development CA/PKCS12 keystore locally, creates namespaces, node-label read permissions, the Retain StorageClass and runtime secrets, and initializes routing to blue only if no routing state exists. It reuses files on repeat runs. Enterprise certificates and a least-privilege proxy identity are later production gates.

## Build and deploy

```bash
make build-images
make deploy TARGET=blue
make deploy TARGET=proxy
make deploy TARGET=green
make status
make forward
```

The scripts obtain ECR URLs from Terraform outputs. Images are baked from local profiles into ECR, tagged with `config.json.image_tag`. ECR tags are immutable; choose a new tag before rebuilding changed profiles. No automatic image push or cloud deployment is performed while validating.

The two directory releases are named `pd` in `identity-blue` and `identity-green`. Green uses blue as the initial seed. All three blue replicas must be ready before other targets deploy. Each colour gets separate StatefulSet claims. The proxy is a stable release in `identity-proxy`, initially routing only to blue. Both colours remain in one replication topology.

After `make forward`, test using a client that trusts `.secrets/ca.crt` and LDAPS `localhost:1636`. Example:

```bash
LDAPTLS_CACERT=.secrets/ca.crt ldapsearch -H ldaps://localhost:1636 \
  -D cn=administrator -y .secrets/root-user-password \
  -b dc=example,dc=com '(objectClass=*)'
```

## Develop a candidate and promote

Edit the server profiles, set a new `image_tag`, validate, build, and deploy only the candidate colour. Follow [docs/RUNBOOK.md](docs/RUNBOOK.md) to verify all domains and writes, create a fresh evidence file, and run:

```bash
make promote TARGET=green EVIDENCE=build/green-evidence.json
```

Promotion persists the active colour before changing all proxy algorithms, so a restarted proxy retains the selected routing state. The evidence file is an **operator attestation**, not a machine proof of zero data loss. The script does not pause writes or delete the old colour/PVCs. Keep both colours replicating until final writes, connection drain and rollback acceptance have been verified.

## Suggested first Codex task

> Read AGENTS.md, README.md and docs/ROADMAP.md. Review the Terraform plan and profile lifecycle against the pinned Ping images. Implement a live validation command that checks every replicated base DN, verifies each server's AZ location, and reconciles acknowledged LDAPS test writes across both colours. Keep validation and deployment separate, and do not apply AWS changes without my instruction.

See [docs/VALIDATION.md](docs/VALIDATION.md) for checks performed while creating this package and the remaining runtime limitations.
