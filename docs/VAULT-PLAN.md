# Local Vault and EKS lifecycle plan

Investigation: 18 October 2026. The user selected keeping the shared Vault
installation unchanged, including HTTP and file storage. The original Raft/TLS
recommendations below are future options, not implementation prerequisites.
Project-specific authentication is configured; importing the retained stack's
secrets remains part of initial adoption after foundation bootstrap.
Current implementation commands are in [LIFECYCLE.md](LIFECYCLE.md).

## Verified local installation

Read-only inspection of context `docker-desktop` on 18 October 2026 found:

- Namespace `hashicorp-vault`, pod `vault-0` ready (2/2).
- Internal endpoint `http://vault.hashicorp-vault.svc.cluster.local:8200`.
  The listener explicitly has `tls_disable = 1`; HTTPS is not currently enabled.
- Vault 1.20.4 is initialized and unsealed, with file storage and HA disabled.
- `data-vault-0` is a bound 10Gi hostpath PVC. A separate 1Gi hostpath
  `vaultkeys-pvc-claim` is present; its contents were not inspected.
- Vault CSI, External Secrets Operator and Vault Secrets Operator are already
  deployed locally. No additional operator is necessary for runner-mediated delivery.
- ARC runner sets in `arc-runners` are repository-scoped. `arc-runner-set` targets
  `DarkEdgesAU/deployPingFederateClients`; the other sets target unrelated repos.
  None of the inspected sets targets `pingdirectory-eks-spot`.

The existing HTTP endpoint is useful for connection discovery, but the planned
credential delivery should use a TLS-protected endpoint with verified trust.
Coordinate listener changes with existing consumers rather than changing this
shared Vault installation as part of an application setup job.

Raft snapshot commands below describe the proposed storage design and **cannot
back up the current file backend**. Before migration, establish a tested,
consistent backup procedure for the current backend and independently preserve
the matching unseal material. Do not switch the storage stanza or initialize a
replacement Vault in place: that does not migrate existing secrets. A supported
storage migration needs its own maintenance and rollback plan. Alternatively,
retain file storage and document/test its consistent offline backup and restore.

Next preparation: create a dedicated repository runner set after the transfer,
confirm its runtime tools and AWS/API connectivity, then configure a least-privilege
Vault OIDC role and a project-specific KV v2 path. No existing auth mounts, secrets,
runner registrations, or Vault configuration were changed by this inspection.

## Recommended first implementation

Run Vault in the persistent local management Kubernetes cluster, independently
of the EKS stack that is destroyed overnight. Use the official Helm chart, TLS,
Integrated Storage (Raft) and a persistent volume. Do not use dev mode. One Raft
node is sufficient for a lab accepting downtime; three pods on one physical host
do not protect against that host failing. Confirm the local storage survives
cluster restarts and document what a local cluster reset would erase.

Start with the local GitHub runner retrieving secrets from Vault and installing
the existing Kubernetes Secrets during setup. This avoids requiring a permanent
network route from EKS to the local network. The runner needs a stable private
HTTPS endpoint, trusted CA and connectivity to both Vault and the EKS API.
Port-forwarding is useful for initial administration, not an unattended endpoint.

EKS pods continue reading their existing Secrets and mounted files. Once installed,
those Secrets remain usable if local Vault is offline; new restoration and secret
refresh require Vault or an explicitly selected encrypted recovery backup.

## Authentication and access

Use GitHub-issued OIDC JWTs with Vault JWT authentication and short-lived tokens.
Bind roles to the actual repository/owner IDs, protected environment, allowed
branch and trusted workflow; verify the claims after the organisation transfer.
Require the intended audience. Give restore jobs read access only to their runtime
paths, and use separate narrowly scoped roles for import and Raft backup.

Do not place a root Vault token in GitHub Secrets. Restrict privileged workflows
to reviewed code: a self-hosted runner for a public repository must not execute
untrusted pull requests with secret access. Continue using AWS OIDC directly for
AWS permissions; AWS authentication should not depend on Vault being recoverable.

## What to store

Use KV v2 for the existing static secrets, importing the original bytes:

| Material | Handling |
| --- | --- |
| Directory passwords, encryption passphrases, PKCS12 stores and PINs | Preserve identities and keys matching retained data volumes |
| PF/PA admin credentials, system keys, JWKs and runtime variables | Preserve existing encodings and native configuration relationships |
| OAuth client secrets, private JWKs and generated credential values | Import existing values; do not regenerate during recovery |
| Licenses and DevOps credentials | Separate policies; grant only to jobs that need them |
| Logging, Grafana and other platform runtime credentials | Maintain current Kubernetes Secret names and namespaces |
| Internal CA private key | Separate restricted bootstrap path; do not distribute it to application pods |

A suggested layout is `kv/ping-eks/lab/runtime/<namespace>/<secret-name>` with
one versioned record per Kubernetes Secret, and separate restricted bootstrap
paths for CA material. Kubernetes Secret `data` values are already base64 encoded;
define that representation explicitly and avoid double encoding binary files or
PF variables. Vault's encryption does not make Kubernetes Secret storage encrypted
automatically; retain namespace RBAC and configure EKS encryption appropriately.

Keep runtime values out of Terraform resources/data sources where they would
enter plans or state. Configure policies and authentication separately from secret
content. Install Secrets through an API or stdin without command-line values,
debug output, ordinary Actions artifacts or temporary plaintext files.

## Compatibility with this repository

The initial adapter should preserve the current Secret contracts: `pd-runtime`,
per-colour PF/PA runtime Secrets, logging credentials/TLS, platform credentials
and optional license/DevOps inputs. Inventory actual names before import.

Existing secret generators must gain an explicit Vault mode that refuses to
regenerate or overwrite imported identities. Existing recovery bundles must remain
usable during migration. Directory's init copy into `/run/ping-secrets` should
remain compatible; changing a Kubernetes Secret does not automatically refresh
files copied at startup. Apply certificate or credential updates through deliberate
application-aware rollouts, preserving directory replication and active routing.

Vault PKI can issue internal certificates later. First preserve the existing CA,
trust stores, aliases and private keys; moving to PKI is a separate migration.

## Optional continuous synchronization

If permanent private connectivity is available, evaluate External Secrets Operator
or HashiCorp Vault Secrets Operator in EKS to synchronize KV records into native
Secrets. This matches existing mounts more closely than adding an Agent sidecar
to every Ping pod. Choose one Secret owner; generators and an operator must not
compete. Configure and test retention of the last good Secret during Vault outages.

Cross-cluster Kubernetes authentication requires Vault to reach the EKS TokenReview
API and manage reviewer credentials. JWT authentication against the EKS issuer
requires updating issuer configuration after cluster recreation. These bootstrap
dependencies must be resolved before adopting direct synchronization.

## Recovery requirements

Take supported Vault Raft snapshots and copy them off the local cluster to the
persistent encrypted backup store. A PVC alone is not a Vault backup. Keep snapshot
access and its AWS/KMS permissions independent of Vault. Store manual unseal shares
or auto-unseal recovery material outside Vault; an AWS auto-unseal KMS key must live
in the persistent foundation and survive every EKS teardown.

Every application backup manifest must record the exact KV versions used by that
backup. Retained Directory data can require its original encryption password and
PF/PA data its original keys. Restoring the latest rotated secret can be incorrect.
Do not purge KV versions still referenced by retained backups. An encrypted export
of required runtime secrets provides a recovery route when local Vault is lost;
test its restoration rather than assuming a Raft snapshot alone is sufficient.

## Implementation and acceptance

1. Confirm local Kubernetes, durable storage, Vault endpoint and runner connectivity.
2. Prepare pinned Helm values, TLS trust, initialization/unseal instructions,
   scoped policies and OIDC roles without secret content in Git.
3. Import existing files/Secrets without regeneration; compare byte digests without
   printing values. Record KV versions and preserve the existing encrypted backups.
4. Implement an offline-testable runner adapter and generator ownership guards.
5. Test secret restoration into an isolated local namespace, Vault outage behavior,
   and recovery from an off-cluster Raft snapshot and independent unseal material.
6. Integrate the protected manual EKS setup/teardown workflows, then rehearse against
   EKS only when explicitly requested. Do not enable automatic credential rotation.

## References

- [Vault Helm deployment](https://developer.hashicorp.com/vault/docs/deploy/kubernetes)
- [GitHub OIDC authentication](https://developer.hashicorp.com/well-architected-framework/secure-systems/secure-applications/ci-cd-secrets/github-actions)
- [Raft snapshots](https://developer.hashicorp.com/vault/docs/sysadmin/snapshots/save)
- [External Secrets Vault provider](https://external-secrets.io/latest/provider/hashicorp-vault/)
- [Vault Secrets Operator](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso)
