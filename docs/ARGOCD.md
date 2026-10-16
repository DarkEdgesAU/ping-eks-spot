# Argo CD deployment management

Argo CD serves `https://argocd.eks.darkedges.com` behind Cloudflare and a public
ALB. Chart 10.4.1 / Argo CD v3.5.2 is vendored and checksum-verified. AWS Terraform
continues to own infrastructure; the ALB controller and Argo CD are bootstrap
Helm releases. Argo manages seven **manual-sync** Applications: Directory blue,
Directory green, Directory Proxy, Federate blue/green and PingAccess blue/green.

## What is managed

`gitops/config.json` contains non-secret repository/revision, ECR and ingress
configuration. `make gitops-export` renders the existing pinned Ping chart into
the tracked `gitops/workloads/` directories and creates `gitops/applications/`.
Argo reads these plain manifests; it does not execute local Python, need ignored
config files, or clone a floating server profile. Review and commit exports with
their source profile/image changes. Build/push immutable images separately.

Use annotation-based Argo resource tracking so the chart's existing Helm labels
and cluster discovery selectors remain intact. The AppProject permits only this
repository and seven workload namespaces, no cluster-scoped resources or Secrets.
The following remain outside Argo's reconciliation:

- Terraform AWS infrastructure and PF management-API configuration.
- Runtime Secrets, licenses, CA keys, and namespace/storage/RBAC bootstrap.
- `pd-routing` and PF grouped ALB routing rules, owned by gated promotion.
- Initial controller/Argo bootstrap and optional PF ingress installed separately.

Applications have no automated sync or cascading deletion finalizer. Exported
StatefulSets have `Prune=false,Delete=false`, PVC retention is explicit, and
existing StatefulSet replica counts are ignored with `RespectIgnoreDifferences`.
Do not enable pruning, force sync, Replace, or cascading deletion. These guards
do not make arbitrary administrative edits safe or replace topology retirement.

## Bootstrap

Keep `revision` on the branch containing the reviewed exports; it currently
points to `feature/pingfederate`. After merging, deliberately move it to `main`
and regenerate/register Applications. Private repository credentials are supplied
through Argo's repository connection UI/Secret, never committed.

Provision AWS through the existing reviewed Terraform workflow with
`enable_federate=true`, refresh kubeconfig, supply licenses, and bootstrap
runtime namespaces/secrets before syncing any workload:

```bash
make kubeconfig
make secrets
make pf-secrets
make pf-controller
make argocd-secrets
make argocd-bootstrap
make gitops-export
# Review, commit and push gitops/ and all supporting source changes.
make argocd-applications
```

These are explicit live commands. Export/validate alone do not deploy, apply
Terraform, push images or create DNS. Do not push raw build/ or .secrets/.

`argocd-secrets` uses the existing internal CA to issue server and repo-server
certificates; only leaf keys are installed, never the CA key. Repo-server TLS
verification is strict. Dex is disabled initially to avoid making deployment
management depend on PF before PF exists. Argo authentication remains enabled;
Cloudflare Access is an additional gate, not an Argo administrator identity.

For initial access, port-forward and trust the internal CA:

```bash
kubectl -n argocd port-forward service/argocd-server 8443:443
```

Visit `https://localhost:8443`. Obtain the generated initial admin password using
`argocd admin initial-password -n argocd`, then change it. Do not place passwords
in Git, command-line arguments or copied logs. Cloudflare/ALB use HTTPS; use the
CLI with gRPC-web through the external HTTP ingress, or use the local forward.
An Access-protected CLI also needs appropriate Access authentication.

## Public endpoint and Cloudflare

In `gitops/config.json`, set an ACM origin certificate ARN in ap-southeast-2
covering **argocd.eks.darkedges.com**, current official Cloudflare IPv4 ranges,
and `access_policy_configured=true` once the existing Access policy covers that
hostname. The PF origin certificate may need an additional SAN; a certificate
covering only admin/id is insufficient. Argo public ingress remains disabled
until these inputs are supplied:

```bash
make argocd-ingress
kubectl -n argocd get ingress
```

In `E:\development\projects\cloudflare\cloudflare-ssl`, configure a proxied
CNAME `argocd.eks` pointing to that ALB's DNS name. Reuse existing Access policy
IDs rather than creating a new allowlist. Ensure the edge certificate covers
`*.eks.darkedges.com` or the exact hostname and use Full (strict); ordinary
`*.darkedges.com` coverage does not cover this name. Disable caching. Preserve
the Cloudflare project's certificate-rotation procedure; never edit the live
pack in place. Source IP restrictions are not tenant authentication: add
account-specific origin mTLS or Access-JWT validation for origin-bypass
protection as described in the PF runbook.

The optional companion `eks.tf` has been added to that Cloudflare project;
[integrations/cloudflare/README.md](../integrations/cloudflare/README.md) records
its inputs and a matching copy. Empty defaults create no resources. Supply the
actual ALB target and existing reusable policy IDs, then review a Cloudflare
plan before applying. The companion validates against the project's locked
provider 5.22.0 in an isolated local copy. The project's existing backend was
not reinitialized or migrated during this work.

Argo server and repo-server use CA-signed internal TLS. ALB backend HTTPS is
encrypted but does not validate backend certificates. Redis follows the chart's
private-cluster default transport; this is not a claim of mTLS on every Argo
control-plane connection.

## Deployment order and handover

PF now uses `identity-federate-blue` and `identity-federate-green`. For an
existing shared-namespace deployment, disable auto-sync and follow
[FEDERATE-NAMESPACE-MIGRATION.md](FEDERATE-NAMESPACE-MIGRATION.md) before updating
Applications. The migration retains admin data via stopped-volume snapshots
and pre-bound restored claims. Do not sync new admins onto empty PVCs or prune
the old namespace. Certificate reissue and routing cutover require verification.

Sync and verify **pd-blue first**, then pd-proxy, then pd-green. Green still joins
blue's replication topology. Sync pf-blue, configure it with the separate PF
Terraform stack, replicate config, and verify it before enabling PF ingress.
Only then deploy/configure the candidate pf-green.

Each later change is: new immutable image tag, build/push, export, review/commit,
then manually sync only the candidate Application. Local `make deploy` and
`make pf-deploy` refuse after `argocd-applications` sets
`config.json.deployment_manager=argocd`. Every operator checkout must preserve
this setting. Argo does not perform the scripts' readiness/topology gates before
a manual sync; follow the ordered runbook and verify actual replication.

For an existing Helm installation, first compare the Git export against live
objects. Register Applications without syncing, inspect their diffs, then adopt
the unchanged resources one application at a time. Do **not** uninstall Helm
releases to hand over: that would delete running workloads. Keep their release
history as recovery metadata and stop using Helm for those workloads. Argo uses
the same object names/namespaces and annotation tracking. Do not change the
storage class, claim templates or service identities during handover.

Gated `make promote` and `make pf-promote` remain explicit runtime operations.
Argo never reconciles their active routing objects back to a Git default.
Before rollback, verify compatibility and replication; syncing old manifests
alone is not a safe routing rollback.

## Validation and teardown

`make validate` renders all seven Ping workloads plus the pinned Argo chart,
checks protected exports, and runs boundary tests. This does not exercise live
adoption, Cloudflare authentication, controller readiness or ALB convergence.

Remove public Ingresses while EKS/controllers still run, and wait for all ALBs
and target groups to disappear. Disable Applications/non-cascading-delete them
before manually retiring workloads, to prevent re-creation during teardown.
Dispose of retained volumes only with explicit data authorization. Remove the
Cloudflare CNAME separately and preserve cryptographic material needed for any
retained data.

References: [Argo ingress](https://argo-cd.readthedocs.io/en/stable/operator-manual/ingress/),
[TLS](https://argo-cd.readthedocs.io/en/stable/operator-manual/tls/),
[sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/).
