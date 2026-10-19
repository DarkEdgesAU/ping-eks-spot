# Organisation transfer and repeatable EKS lifecycle

Investigation and implementation: 18 October 2026. The repository transfer is
complete, branch/environment protections are configured and the dedicated local
runner set is registered. GitHub App access, AWS SSO profile `pingfeddeploy` and
project-specific Vault authentication are configured. No AWS infrastructure
was applied and no lifecycle job was executed. Current commands and implementation
limits are in [LIFECYCLE.md](LIFECYCLE.md); the remainder records the original design.

## Verified GitHub prerequisites

- Transferred repository: `DarkEdgesAU/ping-eks-spot`, public, default branch `main`.
- The authenticated account has repository admin access and is an active
  administrator of `DarkEdgesAU` (GitHub Team).
- Repository ID remains `1415576809`; organisation ID is `299816438`.
- Dedicated ARC runner registration awaits installation 154102616 repository access.
- PR #2 is merged. New lifecycle workflows are on the implementation branch.

A transfer is feasible based on these checks. After transfer, update the Git
remote, GitOps source URLs/AppProjects and ignored local GitOps configuration.
GitHub redirects old repository URLs, but use the canonical organisation URL.
Review Actions permissions, environment protection and runner-group access after
transfer; do not assume an existing runner elsewhere becomes available.

Reference: https://docs.github.com/en/repositories/creating-and-managing-repositories/transferring-a-repository

## Proposed controls

Keep pull-request validation on GitHub-hosted runners. Run AWS lifecycle jobs
only through `workflow_dispatch`, on the protected default branch, using a
restricted runner group and a protected GitHub environment. Serialize lifecycle
jobs with one concurrency group and `cancel-in-progress: false`; do not interrupt
a running backup/destroy operation just because another request arrives.

The runner must be outside the EKS stack it destroys. Confirm its OS, labels,
registration scope, network route to the EKS API and egress address before
writing `runs-on` and API access configuration. Public repositories cannot use
organisation runner groups by default; explicitly restrict privileged workflow
access rather than assigning the runner to pull-request jobs.

Use GitHub OIDC for AWS authentication. Keep the OIDC provider and IAM role in
the persistent foundation, outside the disposable EKS state. Restrict the trust
policy to this repository and the lifecycle environment. GitHub documents an
immutable owner/repository-ID subject format for transfers after July 15, 2026;
verify the actual post-transfer subject format before provisioning trust.

References:
- https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access
- https://docs.github.com/en/actions/reference/security/oidc
- https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws

## Persistent foundation versus disposable infrastructure

Persistent resources:

- Versioned, encrypted S3 Terraform backend with state locking.
- Private encrypted backup bucket, bundle manifests and completed-backup markers.
- Any customer-managed KMS keys needed to decrypt those objects or EBS snapshots.
- GitHub OIDC deployment roles and the four existing ECR repositories/images.
- Retained EBS volumes and completed recovery snapshots.

Disposable resources:

- EKS, worker groups, runtime IAM associations, ALBs and application networking.
- VPC, subnets, NAT gateways and EIPs, after their dependencies are removed.

The AWS Terraform root currently uses local state and owns ECR repositories with
`force_delete=false`. An unattended `terraform destroy` will not cleanly remove
non-empty ECR repositories. Migrate ownership to the persistent foundation using
a reviewed state migration without deleting/recreating repositories. Keep that
foundation outside the EKS destroy command.

S3 state locking can use `use_lockfile=true`; backend versioning provides state
recovery. Do not place AWS credentials in backend configuration or plan artifacts.
Reference: https://developer.hashicorp.com/terraform/language/backend/s3

## Shutdown workflow

1. Verify AWS account `447061058131`, region `ap-southeast-2`, cluster
   `pd-spot-dev`, expected Kubernetes context and exclusive lifecycle lock.
2. Capture inventories for all deployed supported namespaces, PVs/PVCs, runtime
   routing and current configuration. Include Directory, Proxy, both PF/PA
   colours, PingCentral, Delegator, logging, monitoring and Argo as applicable.
3. Preserve CA/private keys, encryption passphrases, PF/PA host/system keys,
   licenses, OAuth client secrets/private JWKs and original runtime Secrets.
   Upload these only as an encrypted private backup bundle, never as ordinary
   GitHub Actions artifacts, Git content or console output.
4. Prepare application-consistent backups/quiescence where required. Verify
   snapshot completion and the backup manifest before destructive work. A live
   EBS snapshot alone is crash-consistent and does not establish application
   consistency or a zero-loss guarantee.
5. Keep original data volumes by default, with snapshots as an independent
   recovery option. Tag retained resources with platform/backup identity and
   record volume IDs, snapshot IDs, AZs, PVC identities and encryption keys.
6. Remove load balancers and runtime networking before destroying EKS/VPC
   resources. Preserve data volumes/PVC identities and images. Do not perform
   Directory topology retirement or delete its PVCs to shut the lab down.
7. Run the reviewed EKS destroy plan against remote state. Record partial
   failures and allow a retry; do not mark the shutdown complete merely because
   the Terraform command exited.
8. Verify EKS, workers, ALBs, NAT gateways and EIPs are gone, while retained
   volumes, snapshots, ECR and backup objects remain. Write a completed result
   marker with inventory checksums and state identity.

Snapshots-only shutdown should be a separate explicit mode, if chosen later;
volume deletion must not be the implicit result of the normal shutdown action.

## Restore/setup workflow

1. Select an explicit completed backup ID; validate its checksums, account,
   region, encryption-key availability and current lifecycle state.
2. Apply the disposable infrastructure from remote Terraform state. Authenticate
   the runner with OIDC and configure its kube context.
3. Reuse saved volumes in their original AZs. For snapshot recovery, create new
   encrypted volumes in the recorded AZs and substitute the new volume handles
   in prepared PV bindings. Preserve PVC names, namespaces, sizes and node affinity.
4. Restore namespaces, runtime Secrets, routing state and retained PV/PVC
   bindings before starting stateful workloads. Use the existing CA and
   application encryption keys; do not generate replacement identities.
5. Bootstrap controllers and Argo, then sync committed workloads in dependency
   order. Preserve the selected active colours. Green must rejoin the original
   Directory topology, not initialize an independent genesis topology.
6. Verify readiness, replication, LDAP reads/writes, PF/PA configuration and
   application state. Report which volumes were reused versus recreated.

The existing `scripts/recovery.py` is a useful binding/Secret preparation layer,
but it does not currently collect backups, upload encrypted bundles, create
snapshots or orchestrate teardown/restore. Those need implementation and tests
before a workflow can safely invoke them.

## Implementation sequence

Local Vault can supply runtime secrets without belonging to the disposable EKS
stack. The proposed runner authentication, migration and recovery design is in
[VAULT-PLAN.md](VAULT-PLAN.md). Backup manifests must preserve the secret versions
that match their retained application data.

1. Confirm runner location/labels and retention mode.
2. Transfer the repository and update canonical GitOps URLs.
3. Add an isolated persistent foundation and migrate existing ECR/state ownership.
4. Add offline-testable backup, shutdown and restore orchestration with resumable
   progress, explicit backup IDs and sensitive-output controls.
5. Add protected manual workflows with OIDC and concurrency controls.
6. Review plans, bootstrap the foundation explicitly, and rehearse one complete
   restore/shutdown cycle against the lab before adding any schedule.

Outstanding: runner registration/OS/network, chosen retention mode, persistent
backend/backup resources and OIDC IAM configuration. No schedules are implied.
