# PingDirectory EKS Spot development project

Optional Prometheus/Grafana, Delegated Admin and PingCentral preparation and the
ordered startup commands are in [docs/PLATFORM-SERVICES.md](docs/PLATFORM-SERVICES.md).
Start with `make platform-render`; no AWS deployment is performed by rendering.

The minimal OpenSearch log-shipping POC is documented in
[docs/OPENSEARCH.md](docs/OPENSEARCH.md). Run `make logging-local-test` to prove
file and container-log collection locally without rebuilding AWS infrastructure.

PingAccess blue/green namespaces, profiles, Argo CD applications and public ALB
ingress are described in [docs/PINGACCESS.md](docs/PINGACCESS.md). Start with
`access.example.json` and `make pa-render`; runtime secrets, image publication,
licensed cluster startup and application configuration are explicit deployment
steps. The PA starter does not yet configure CA-signed internal listeners or a
working protected application. The repeatable PingFederate runtime proxy at
`https://access.eks.darkedges.com/identity/` is configured with
`make pa-identity-plan`, `make pa-identity-apply` and `make pa-identity-verify`
per colour; see [the integration steps](docs/PINGACCESS.md#front-pingfederate-at-identity).

A workspace for Terraform-managed EKS, PingDirectory and PingFederate profiles,
and Argo CD deployments across three AWS Availability Zones. Argo CD manages
versioned workloads; explicit verification gates control blue/green promotion.

The packaged default runs directory and proxy workloads on **Spot**. Each directory replica has its own retained EBS volume. This is a runnable development project with local validation; it is **not a tested production RPO=0 deployment**. Cross-AZ write acknowledgment, live profile/bootstrap validation, and failure testing remain explicit gates in [docs/ROADMAP.md](docs/ROADMAP.md).

## Included layout

| Path                           | Purpose                                                                            |
| ------------------------------ | ---------------------------------------------------------------------------------- |
| `terraform/`                   | VPC, per-AZ networking and Spot groups, EKS, access, Pod Identity, EBS driver, ECR |
| `profiles/pingdirectory/`      | Editable server profile, base DN, assurance example and AZ setup hook              |
| `profiles/pingdirectoryproxy/` | Trusted TLS backends and explicit blue/green routing                               |
| `profiles/pingfederate/`       | Adapted Ping baseline identity profile with runtime secrets                        |
| `terraform-federate/`          | Per-colour LDAP and basic PingFederate configuration                               |
| `gitops/`                      | Argo CD configuration, Applications and exported workload manifests                |
| `scripts/gitops.py`            | Argo bootstrap and secret-free GitOps export                                       |
| `images/`                      | Bake profiles into versioned Ping images; no Git credentials needed in pods        |
| `scripts/project.py`           | Render/deploy, secrets, image builds, status, gated promotion                      |
| `.devcontainer/`               | Linux x86-64 development tools for a remote VS Code/Docker host                    |
| `AGENTS.md`                    | Project instructions for Codex                                                     |
| `tests/`                       | Promotion boundary tests                                                           |
| `docs/`                        | Architecture, cutover/recovery runbook and production acceptance work              |

Baseline: Ping chart **0.16.0**, DevOps images **2609**, PingDirectory/Proxy **11.1.0.1**. Terraform **1.13.5**, Helm **3.19.0**, EKS/Kubectl **1.35**. The chart is vendored with its official SHA-256 checked before rendering. The AWS provider is recorded in `terraform/.terraform.lock.hcl` after validation. Keep version changes deliberate; `latest` is not an image tag in the project.

Argo CD uses chart **10.4.1** / application **3.5.2**. PF configuration pins
provider **1.10.0**; confirm the configured PF product version against your
licensed image before deployment.

## Remote computer setup

For PingFederate admin/engine ingress, shared internal CA and independent
blue/green deployments, see [docs/PINGFEDERATE.md](docs/PINGFEDERATE.md).
For Argo CD workload management at `argocd.eks.darkedges.com`, see
[docs/ARGOCD.md](docs/ARGOCD.md). Argo mode replaces direct workload Helm deploys
with reviewed Git exports and manual candidate syncs; promotion remains gated.

Assumption: a Linux x86-64 remote computer with Docker, or VS Code Remote SSH and Dev Containers. The infrastructure still runs in your AWS account; the remote computer is the development workstation. ARM hosts need adjusted tool downloads/emulation.

1. Unzip the project and open its directory with your existing Codex setup. `AGENTS.md` supplies repository guidance.
2. For VS Code, open the remote directory and choose **Reopen in Container**. Create `~/.aws` first because it is mounted for your AWS CLI profile. The Docker feature uses the remote host's daemon.
3. Alternatively install Python 3, Terraform, Helm, AWS CLI v2, kubectl, Docker, OpenSSL, Java keytool, the Argo CD CLI and make directly. Use a Python virtual environment, then run:

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

The remaining command blocks use Bash/WSL syntax. In PowerShell, replace direct
`python3` calls with `py -3` or your Windows virtual-environment interpreter;
continue passing `PYTHON=.venv/Scripts/python.exe` to make if needed.

## Configure and provision

Edit `config.json`, copy `federate.example.json` to ignored `federate.json`, and
copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars`.
Set your IAM role/user ARN and workstation egress CIDR, and set
`enable_federate=true` to provision the ALB controller's AWS integration.
Defaults use **ap-southeast-2** and three AZs. Review the Ping EULA before
setting `config.json.accept_eula=true`.

Copy `gitops/config.example.json` to ignored `gitops/config.json`: confirm the
ECR prefix, repository URL, revision, origin certificate and Cloudflare ranges.
The example targets `main`; merge deployment changes before syncing that revision.
Set immutable image tags in `config.json` and `federate.json` before building.

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

## Supply licenses and bootstrap secrets

Supply all three authorized license files, with these exact filenames:

- `.secrets/PingDirectory.lic`
- `.secrets/PingDirectoryProxy.lic`
- `.secrets/PingFederate.lic`

Keep licenses and private key material out of Git. License download is not automated.

```bash
make secrets
make pf-secrets
make argocd-secrets
```

Run these in order. They reuse the shared internal CA, generate product/runtime
secrets and Argo server/repo-server certificates, and create the required
namespaces and retained storage configuration. The CA private key stays local.
Directory routing initializes to blue only when no routing state exists.
Existing encrypted data requires its original secret material; do not regenerate
keys over retained instances.

## Build images and bootstrap Argo CD

```bash
make build-images
make pf-build-image TARGET=blue
# Build green separately only if its tag differs from blue's.
make pf-controller
make argocd-bootstrap
make gitops-export
```

Image build commands publish to ECR and require AWS authentication. Directory
images use `config.json.image_tag`; Federate uses per-colour tags in
`federate.json`. ECR tags are immutable: do not rebuild/push an existing tag.
`make gitops-export` writes secret-free manifests and Applications under
`gitops/`; export and validation do not deploy or publish anything.

Review, commit and push the source changes and `gitops/` to the configured
repository/revision, then register the Applications:

```bash
make argocd-applications
kubectl -n argocd port-forward service/argocd-server 8443:443
```

Keep the forward running and visit `https://localhost:8443`, trusting the
internal CA. In another terminal, obtain the generated initial password with
`argocd admin initial-password -n argocd`, log in as `admin`, and change it.
For a private Git repository, configure repository credentials in Argo CD.
Registration does not sync workloads. It sets local
`config.json.deployment_manager=argocd`, which disables direct `make deploy`
and `make pf-deploy`; preserve that setting in every operator checkout.

## Sync workloads in order

In Argo CD, manually sync one Application at a time and verify it before
continuing. Leave pruning, force/Replace sync and cascading deletion disabled.

1. `pd-blue`: wait for all three directory replicas to become ready.
2. `pd-proxy`: verify LDAPS routing to blue.
3. `pd-green`: verify it joins blue's topology and replication catches up.
4. `pf-blue`: wait for the admin and all three engines, then configure it below.
5. `pf-green`: deploy and configure after blue has been verified.

Both PF colours can initially reference the same built image tag. If green
uses a different tag, build it with `make pf-build-image TARGET=green` before
syncing `pf-green`.

Directory objects retain the `pd` naming in `identity-blue` and `identity-green`.
Each replica has a separate retained claim; both colours remain in one topology.
The proxy runs in `identity-proxy`. PF colours have separate admin/engine
clusters in `identity-federate-blue` and `identity-federate-green`. For the
existing shared-namespace lab, follow the configuration-preserving
[namespace migration](docs/FEDERATE-NAMESPACE-MIGRATION.md) before syncing.
Argo Applications do not manage runtime secrets
or active-colour routing. Argo sync does not run the scripts' topology checks;
the verification order above remains an operator responsibility.

For an existing Helm deployment, follow the adoption procedure in
[docs/ARGOCD.md](docs/ARGOCD.md) before the first sync. **Do not uninstall Helm
releases to transfer ownership**, because that would delete running workloads.

### Configure PingFederate with Terraform

After syncing `pf-blue`, run from the repository root:

```bash
python3 scripts/federate_config.py plan --target blue
# Review the saved candidate plan.
python3 scripts/federate_config.py apply --target blue
python3 scripts/federate_config.py replicate --target blue
make pf-status
```

The wrapper owns a direct candidate admin port-forward, selects a separate
Terraform workspace, and refuses to configure the active PF colour. Verify the
LDAP connection and configuration on every engine. Repeat with `--target green`
after syncing green. Configure initial blue before enabling PF ingress, which
initializes its active routing. See [docs/PINGFEDERATE.md](docs/PINGFEDERATE.md)
for acceptance checks and session/token limitations.

### Verify Directory LDAPS

```bash
make status
make forward
```

After `make forward`, test using a client that trusts `.secrets/ca.crt` and LDAPS `localhost:1636`. Example:

```bash
LDAPTLS_CACERT=.secrets/ca.crt ldapsearch -H ldaps://localhost:1636 \
  -D cn=administrator -y .secrets/root-user-password \
  -b dc=example,dc=com '(objectClass=*)'
```

## Publish ingress through Cloudflare

| Hostname                   | Service             |
| -------------------------- | ------------------- |
| `argocd.eks.darkedges.com` | Argo CD             |
| `pingfederate-admin.eks.darkedges.com`  | PingFederate admin  |
| `id.eks.darkedges.com`     | PingFederate engine |

Supply ACM origin certificate ARNs in ap-southeast-2 covering the relevant
hostnames, and current Cloudflare IPv4 ranges in `gitops/config.json` and
`federate.json`. Reuse your existing Cloudflare Access policies for the admin
endpoints. Once their protection is configured, set
`access_policy_configured=true` for Argo and `admin_access_configured=true` for PF.

```bash
make argocd-ingress
make pf-ingress
kubectl -n argocd get ingress
kubectl -n identity-federate-blue get ingress
kubectl -n identity-federate-green get ingress
```

Use the resulting ALB DNS names in the optional `eks_alb_hosts` configuration
in `E:\development\projects\cloudflare\cloudflare-ssl`, with existing reusable
Access policy IDs. Review that project's plan before applying. Configure proxied
CNAMEs, edge certificate coverage for `*.eks.darkedges.com`, Full (strict), and
disabled caching. The internal CA certificate does not replace the Cloudflare
edge/origin certificates. See [integrations/cloudflare/README.md](integrations/cloudflare/README.md)
and [docs/ARGOCD.md](docs/ARGOCD.md) for configuration and origin protection.

## Develop candidates and promote

Edit profiles, choose a new immutable candidate image tag, validate, build/push,
and run `make gitops-export`. Review, commit and push the updated manifests,
then manually sync **only the candidate Application** in Argo CD. Syncing a
candidate does not promote traffic.

For Directory, follow [docs/RUNBOOK.md](docs/RUNBOOK.md) to verify replication
and writes, create fresh evidence, and run:

```bash
make promote TARGET=green EVIDENCE=build/green-evidence.json
```

Promotion persists the active colour before changing all proxy algorithms, so a restarted proxy retains the selected routing state. The evidence file is an **operator attestation**, not a machine proof of zero data loss. The script does not pause writes or delete the old colour/PVCs. Keep both colours replicating until final writes, connection drain and rollback acceptance have been verified.

For PF, apply and replicate the candidate's Terraform configuration, verify login,
sessions/tokens and rollback compatibility, then generate PF-specific evidence
and use `make pf-promote TARGET=green EVIDENCE=build/pf-green-evidence.json` as
described in [docs/PINGFEDERATE.md](docs/PINGFEDERATE.md). Directory and PF
promotions are independent. Argo does not revert their runtime routing objects.

## Tear down the lab

For exact PowerShell commands to rebuild the infrastructure and rebind retained
data after teardown, follow [docs/RECOVERY.md](docs/RECOVERY.md). Restore original
secrets and bind the eight current data claims before any Argo sync.

Remove public Ingresses while EKS and the ALB controller still run; wait for
ALBs and their dependencies to disappear. Remove Argo Applications without
cascading deletion before retiring workloads, so they cannot recreate them.
Then follow the teardown runbooks before `terraform destroy`. Directory and PF
admin EBS volumes are retained and continue billing until explicitly disposed
of. Remove obsolete Cloudflare DNS records separately and preserve keys needed
for any retained encrypted data. See [docs/ARGOCD.md](docs/ARGOCD.md).

## Suggested first Codex task

For continuous LDAP traffic during a blue/green promotion, see [docs/LOADTEST.md](docs/LOADTEST.md).
The JMeter suite seeds disposable entries, records operations and promotion markers,
and reconciles acknowledged writes directly against all six directory replicas.

> Read AGENTS.md, README.md and docs/ROADMAP.md. Review the Terraform plan and profile lifecycle against the pinned Ping images. Implement a live validation command that checks every replicated base DN, verifies each server's AZ location, and reconciles acknowledged LDAPS test writes across both colours. Keep validation and deployment separate, and do not apply AWS changes without my instruction.

See [docs/VALIDATION.md](docs/VALIDATION.md) for checks performed while creating this package and the remaining runtime limitations.

## Diagrams

### KubeDiagram

```console
kubectl get all,ingress,pvc,sc,configmap,secrets,pvc,node,ServiceAccount,PersistentVolume,CSIDriver -n identity-proxy -o yaml | docker run --rm
 -i -v ${PWD}:/work philippemerle/kubediagrams kube-diagrams -o /work/eks-architecture.png -
 ```

### Terravision

 ```console
 terravision draw --source terraform
 ```


### Cloudflare proxy without Access

Set `proxy_only: true` in both `gitops/config.json` and `federate.json` to explicitly enable public admin ingress without Cloudflare Access. Keep the Access attestation flags false. ACM certificates and Cloudflare source CIDRs remain required; Argo CD and PingFederate retain their own authentication. Use proxied CNAMEs, Full (strict) TLS and cache bypass. Run `make argocd-ingress` and `make pf-ingress`, then inspect `kubectl get ingress -A -o wide`.
