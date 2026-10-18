# Manual EKS lifecycle with local Vault

The repository is now [DarkEdgesAU/pingdirectory-eks-spot](https://github.com/DarkEdgesAU/pingdirectory-eks-spot).
The implementation branch adds protected manual workflows and local equivalents.
AWS foundation bootstrap, Vault project authentication and a live recovery rehearsal
must complete before treating these workflows as operational.

## Management prerequisites

Docker Desktop Kubernetes hosts Vault in `hashicorp-vault` and ARC in
`arc-runners`/`arc-systems`. Vault stays unchanged: HTTP, file storage, unsealed.
The allowed service endpoint is
`http://vault.hashicorp-vault.svc.cluster.local:8200`. Credentials cross this local
connection without TLS. No EKS-to-local-Vault network connection is required.

The dedicated `ping-eks-lifecycle` ARC set uses chart 0.14.2, zero idle runners and
one maximum runner. Its existing GitHub App installation **154102616** needs access
to the transferred repository. Add it at the organisation's Installed GitHub Apps
settings. Installation initially returned 403; Helm installation alone does not
prove successful runner registration.

```powershell
helm --kube-context docker-desktop upgrade --install ping-eks-lifecycle `
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set `
  --version 0.14.2 -n arc-runners -f management/runner-values.yaml
kubectl --context docker-desktop -n arc-runners get autoscalingrunnerset ping-eks-lifecycle
```

`main` now requires the `validate` check and pull requests, with force pushes and
deletion disabled. The `eks-lifecycle` environment allows only `main` and requires
review by `darkedges`. Pull requests use GitHub-hosted runners. Lifecycle jobs are
manual, serialized, cannot cancel one another, and never publish private artifacts.
Review lifecycle code and dependencies before approving an environment deployment.

## Bootstrap Vault project access

Use an existing authorized Vault administrative session. Do not put a root token
in GitHub, terminal history or this repository. From a trusted local terminal,
forward the service in a separate window:

```powershell
kubectl --context docker-desktop -n hashicorp-vault port-forward service/vault 18200:8200
```

In another shell, enter a temporary administrative token at the hidden prompt:

```powershell
$env:VAULT_ADDR = 'http://127.0.0.1:18200'
py -3 scripts/management_bootstrap.py --prompt-token
```

This creates only the project's `ping-eks` KV v2 mount, `ping-eks-github` JWT auth
mount, policies and roles. It refuses incompatible existing mounts. GitHub OIDC
roles bind repository/owner IDs, `main`, the protected environment and the exact
lifecycle workflow. Backup can create/update project versions; restore is read-only.
Neither can delete versions. Existing Vault consumers/storage/listeners stay unchanged.

Application Secrets use `ping-eks/lab/runtime/<namespace>/<secret>`. Original local
bootstrap files, including private CA material and product configuration state, use
the separate `ping-eks/lab/bootstrap/files` record. Application pods never receive
that bootstrap record or Vault tokens. Runtime base64 data is preserved unchanged.

Vault's file backend cannot use Raft snapshots. These workflows independently export
the project's required secrets into encrypted AWS recovery bundles; they do not
back up or migrate the entire shared Vault installation.

## Bootstrap persistent AWS resources

Merge the implementation through validation first. Run **Inspect lifecycle identity**
from GitHub Actions on `main`, approve its environment deployment, and save only its
printed claims JSON to `build/oidc-claims.json`. No JWT is printed. The exact verified
post-transfer `sub` is used for AWS trust; do not guess the old repository-name format.

Renew AWS SSO and select its profile. Existing environment credentials can override
SSO, so resolve any `InvalidClientTokenId` before proceeding. Confirm account:

```powershell
aws sso login --profile YOUR_PROFILE
$env:AWS_PROFILE = 'YOUR_PROFILE'
aws sts get-caller-identity
py -3 -m pip install -r requirements.txt
py -3 scripts/foundation_bootstrap.py --claims build/oidc-claims.json
terraform "-chdir=foundation" show ../build/foundation.tfplan
```

Review `foundation/deployment-policy.example.json` against your account before
bootstrap; pass a reviewed alternative with `--policy`. It scopes runtime IAM names
and regional infrastructure access and denies volume/snapshot/ECR/bucket deletion
and KMS deletion scheduling. Live IAM permissions remain an acceptance check.

The prepared plan imports existing ECR repositories declaratively, creates private
encrypted/versioned buckets and a persistent KMS key/role, and reuses an existing
GitHub OIDC provider or creates one. EKS is not created by this plan.

```powershell
terraform "-chdir=foundation" apply ../build/foundation.tfplan
py -3 scripts/configure_lifecycle.py
terraform "-chdir=foundation" init -migrate-state "-backend-config=backend.hcl"
terraform "-chdir=terraform" init -migrate-state "-backend-config=backend.hcl"
```

Keep the local state backups until both migrations are verified. `foundation/`
owns the repositories; `terraform/` has non-destructive removed blocks for their
old addresses and reads them as data sources. Never destroy `foundation/` as part
of a nightly shutdown. Do not commit state, plans, private bundles or local settings.

Set environment variables in GitHub from the generated non-secret configuration:

```powershell
$role = terraform "-chdir=foundation" output -raw role_arn
gh variable set AWS_LIFECYCLE_ROLE_ARN --env eks-lifecycle --body $role
Get-Content lifecycle.json -Raw | gh variable set LIFECYCLE_CONFIG --env eks-lifecycle
```

The runner's current internet egress IP must be included in the saved infrastructure
`api_public_access_cidrs`. The workflow sets `TF_VAR_lifecycle_principal_arn` so the
OIDC role gets its own EKS API access entry. Persistent foundation resources accrue
storage/KMS charges; retained volumes and snapshots continue accruing storage charges.

## Adopt the existing retained stack

Since EKS is already destroyed, the first backup uses an existing teardown inventory
instead of querying a nonexistent cluster. Use the newest verified inventory and
its matching local keys/configuration. Adoption never generates replacement secrets.

```powershell
py -3 scripts/lifecycle.py adopt --backup-id retained-20261016 `
  --inventory .secrets/teardown-20261016T053446Z
```

Run adoption with the scoped Vault session and valid AWS profile. It imports exact
secret values, saves a KMS-envelope-encrypted bundle, snapshots all retained EBS PVs
(including legacy retained volumes), waits for completion and publishes an immutable
completed manifest. Legacy `identity-federate` volumes are preserved but not deployed.
No EKS resources start during adoption.

Use fresh backup IDs for new captures. A retry with the same incomplete ID reuses
its frozen encrypted bundle and already-created snapshots. A completed ID is immutable.
Independent backup exports preserve the exact bytes even if a referenced Vault KV
version is later removed; use explicit encrypted-backup recovery in that case.

## Restore and shutdown

Local dry-run, which does not connect to AWS or Vault:

```powershell
make lifecycle-plan BACKUP_ID=retained-20261016
```

In GitHub Actions choose **EKS lifecycle**, operation `setup`, and completed backup
ID `retained-20261016`. Approve the environment. Setup restores namespaces, Secrets,
AZ-bound retained PV/PVCs and routing before ordered Argo synchronization. It uses
the saved seed colour, then its peer, proxy, PF/PA and optional applications. Setup
plans refuse resource deletion/replacement. Original volumes must be available, or
already bound to this exact target cluster during a retry; no force-detach is used.

For independent recovery, choose `secret_source=encrypted-backup`. This is an explicit
operator choice, not silent fallback when Vault fails. Set `from_snapshots=true` only
to recreate required workload volumes from recorded snapshots in their recorded AZs.
Original volumes remain untouched. Recreated volumes are tagged for resumable retry.

Before shutdown, stop external test traffic. Choose operation `shutdown` with a new
backup ID, for example `night-20261018`. The workflow verifies the backup and completed
snapshots, suspends Argo automatic sync, removes public ingresses and waits for ALB
cleanup before applying a guarded destroy plan. It does not change Directory replica
counts, delete PVCs or retire topology. It verifies EKS, worker instances and the VPC
are gone and retained volumes are detached before reporting completion.

Equivalent local operations (AWS/Vault credentials and correct configuration required):

```powershell
make lifecycle-backup BACKUP_ID=manual-20261018
make lifecycle-shutdown BACKUP_ID=night-20261018
make lifecycle-setup BACKUP_ID=night-20261018
```

The default snapshot mode is explicitly **crash-consistent**. It does not establish
application consistency or zero loss across independently captured volumes. Retained
original volumes are the normal restore source. Application-consistent captures require
reviewed native backup/quiescence commands in `pre_backup_commands`; changing the label
alone is refused. Verification commands are configurable and must include the relevant
application checks; the example starts with Directory replication and product status.
Setup also adds a disposable LDAP entry through the proxy, waits for it on all six
Directory replicas, and deletes it. This probe does not establish a zero-loss guarantee.

Public ingress definitions and selected colours are restored, but ALB DNS names can
change. Review new ingress addresses and update the separate Cloudflare configuration
before claiming external URLs have recovered. No Cloudflare API credentials are stored
or DNS records changed by these workflows.

## Failure handling and acceptance

Snapshot, checksum, Vault, identity or verification failures stop the operation without
claiming completion. Re-run the same ID after fixing the cause. Setup and teardown have
no automatic rollback that would delete retained data. If the runner is killed, the
S3 `lifecycle/operation.lock` can remain: confirm no job/process is active before an
administrator removes that one lock object. Never unlock a running operation.

Incomplete destroy can leave dependencies such as orphaned ENIs/security groups. Inspect
and resolve only verified cluster-owned dependencies, then retry; the tool does not
force-delete arbitrary networking. Check AWS resource inventories for any remaining
charges after a failed teardown.

Local checks are `make validate` and `make terraform-check`. Live acceptance still needs:
successful OIDC exchanges, runner/EKS connectivity, a complete restore, replication and
LDAP write/read checks, PF/PA configuration checks, ingress routing, and another verified
backup/shutdown. There are no schedules or automatic credential rotations.
