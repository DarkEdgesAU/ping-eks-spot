# PingFederate blue/green

Two independent clusters in `identity-federate-blue` and `identity-federate-green`, each with one retained-volume
admin and three engines across three AZs. Candidate deployment leaves the stable
active grouped ALB ingress rules unchanged. Directory and
Federate promotions are independent. Both PF colours use Directory Proxy LDAPS.

Profiles adapt Ping's `baseline` at commit
`804766b0720d0bcdd2e0b0270b0e711d66e6f601`. Existing directory topology/TLS hooks
remain. Bundled private keys, passwords, OAuth clients with known credentials,
OAuthPlayground binary and delegated-admin extension are excluded. Generated
demo-user passwords replace defaults. PF uses a dedicated ordinary read-only
LDAP entry, not the baseline root account. Write-dependent baseline features
need separately reviewed LDAP permissions. No custom OIDC client is added.

## Configure

For an existing shared-namespace deployment, first follow
[FEDERATE-NAMESPACE-MIGRATION.md](FEDERATE-NAMESPACE-MIGRATION.md). Preserve the
current admin configuration with restored claims before syncing new namespaces.

Copy `federate.example.json` to ignored `federate.json`. Set immutable per-colour
image tags. The image release is 2609, product version 13.1, provider 1.10.0;
confirm the licensed image actually contains that product version before use.
Set `enable_federate=true` in AWS tfvars, then use the existing reviewed AWS
plan/apply workflow. Infrastructure Terraform adds ECR and the official ALB
controller IAM/Pod Identity integration; Kubernetes remains a separate step.

Cloudflare must cover the exact names `pingfederate-admin.eks.darkedges.com` and
`id.eks.darkedges.com`. Ordinary Universal SSL for `*.darkedges.com` does not
cover these deeper names. Configure an appropriate edge certificate or delegated
child zone. Set `origin_certificate_arn` to an ACM certificate in ap-southeast-2
covering both names (Cloudflare Origin CA imported into ACM or a publicly trusted
certificate). Keep its private key outside Git. Use Cloudflare Full (strict).
Populate `cloudflare_ipv4_cidrs` from Cloudflare's current official IP list.

Configure Cloudflare Access with your explicit admin allow policy before setting
`admin_access_configured=true`. Until then public admin ingress stays disabled;
administration uses Kubernetes port-forward. Source IP filtering alone is not
administrator authorization. Add account-specific Authenticated Origin Pulls
with ALB mTLS, or origin Access-JWT validation, before treating the public admin
origin as protected against bypass by another Cloudflare tenant. DNS/Access are
configured manually, not changed by these scripts.

## Bootstrap and configure blue

Deploy fresh Directory blue, proxy and green first with new profile image tags.
An existing directory database needs an explicit LDIF/schema migration; profile
replacement with `--reimportData never` does not seed new service entries. Never
delete PVCs as a migration workaround. This sequence assumes a fresh lab.

Supply authorized Directory/Proxy licenses and `.secrets/PingFederate.lic`:

The Kubernetes Secret keeps the `PingFederate.lic` key; its projected runtime
filename is `pingfederate.lic`, matching the Linux container's license lookup.

```bash
make secrets
make pf-secrets
make pf-controller
make pf-build-image TARGET=blue
make pf-deploy TARGET=blue
terraform -chdir=terraform-federate init
python3 scripts/federate_config.py plan --target blue
# Review the plan before applying.
python3 scripts/federate_config.py apply --target blue
python3 scripts/federate_config.py replicate --target blue
```

The controller is pinned to chart 1.16.0 / app v2.16.0; its official IAM policy
is vendored under `terraform/policies/`. PF configuration Terraform adopts
baseline IDs for LDAP, CA, SSL and server settings. The issuer/base URL is stable
across colours. Credentials come from ignored secret files. Saved Terraform
plans/state contain LDAP secrets: use an encrypted remote backend with locking
before shared use. The wrapper owns a direct candidate port-forward (19999 blue,
29999 green), selects the matching workspace and refuses active-colour changes.
Stop competing forwards on these ports. Do not run simultaneous config commands.
After apply, explicitly replicate config and verify every engine received it.

Verify LDAP and initial login before exposing ingress:

```bash
make pf-status
make pf-ingress
kubectl -n identity-federate-blue get ingress
kubectl -n identity-federate-green get ingress
```

This creates two independent public ALBs when admin Access is enabled. Add
proxied Cloudflare CNAMEs `id.eks` and `pingfederate-admin.eks` to the corresponding ALB DNS
names, and disable caching. Initial routing is created once to blue; later
deploy/ingress commands preserve the active colour.

## CA and TLS

Every PF admin and engine waits in `wait-for-directory-proxy` after copying its
runtime secrets. The init container connects to Directory Proxy on LDAPS 636
and verifies its certificate/hostname using the internal CA. It retries up to
60 times, waiting five seconds between attempts, with a ten-second connection
timeout. After exhaustion it fails; Kubernetes retries the init container and
PF remains stopped. This checks reachability and TLS, not an authenticated LDAP
lookup or replication health. The wait runs at pod creation; a later proxy
outage does not restart PF automatically. No image rebuild is needed for this
init-container change; export, commit and sync the PF Applications.

Directory and PF reuse `.secrets/ca.crt` and `ca.key`. PF gets separate CA-signed
TLS keys per colour, a shared generated signing key, separate master/system keys
and cluster passwords, and a generated admin password. The CA private key is
never mounted. Secret files are copied to memory-backed volumes before startup.
Restart hooks preserve Terraform configuration. Rotate secrets deliberately;
never regenerate master keys over an existing retained admin instance.

Admin/engine HTTPS, the Terraform API and Directory LDAPS use TLS; the provider
and LDAP store enforce certificate/hostname verification. ALB backend HTTPS
encrypts traffic but ALB does not validate target certificates. Native PF JGroups
uses authenticated AES encryption, **not certificate-based TLS**. Strict TLS on
every internal connection still requires a separately validated service mesh or
supported JGroups TLS setup. Validate cluster-password obfuscation with the
pinned licensed server; the offline render does not establish working cluster
encryption. These remain explicit runtime acceptance gates.

## Candidate and promotion

Set green's new immutable tag, then:

```bash
make pf-build-image TARGET=green
make pf-deploy TARGET=green
python3 scripts/federate_config.py plan --target green
python3 scripts/federate_config.py apply --target green
python3 scripts/federate_config.py replicate --target green
```

Verify LDAP, baseline login, config convergence, tokens, sessions, transaction
drain and rollback. Independent clusters do not share in-memory sessions,
reference tokens or authorization codes. A shared signing key does not prove
continuity. Decide and test reauthentication or supported shared state before
promotion; this is not a promise of uninterrupted existing sessions.

Generate a draft, then explicitly attest only completed checks:

```bash
python3 scripts/federate.py evidence --target green --notes "Actual checks and report paths"
python3 scripts/federate.py evidence --target green --attest-verified --notes "Actual completed checks and report paths"
make pf-promote TARGET=green EVIDENCE=build/pf-green-evidence.json
```

The gate requires fresh (five-minute) human evidence matching account/cluster,
image/colour, all six checks, admin readiness and three-AZ engine readiness.
Grouped ingress rules update sequentially, not atomically; ALB target registration/drain is
asynchronous. Check all target groups before declaring completion. A 120-second
deregistration delay is not a session guarantee. Both colours stay deployed.
Rollback uses newly verified blue evidence and the same promotion command.

## Validate and tear down

```bash
terraform -chdir=terraform-federate init -backend=false
make validate
make terraform-check
```

These validate syntax, provider schemas and rendering, not licensed bootstrap,
bulk import conversion, clustering, login, Cloudflare or ALB convergence. Those
require the next explicitly authorized lab deployment.

Delete Ingresses while the controller/EKS still run, and wait for the ALBs and
their dependent resources to disappear. Then uninstall both PF releases and the
controller before destroying AWS. Record retained PF admin EBS volume IDs as
well as directory volume IDs; both need explicit disposal. Remove Cloudflare
records manually. Preserve secrets while retained encrypted data is needed.

References: [Ping profiles](https://github.com/pingidentity/pingidentity-server-profiles),
[PF provider](https://registry.terraform.io/providers/pingidentity/pingfederate/1.10.0/docs),
[ALB annotations](https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.16/guide/ingress/annotations/),
[Cloudflare Origin CA](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/).

## Fixed admin access per colour

`make pf-ingress` also creates host rules for `pingfederate-blue-admin.eks.darkedges.com`
and `pingfederate-green-admin.eks.darkedges.com`. Both use the shared `pf-admin` ALB, but
route directly to their respective namespace's admin service regardless of
promotion. `pingfederate-admin.eks.darkedges.com` continues following the active colour.
Create proxied Cloudflare CNAMEs `pingfederate-blue-admin.eks` and `pingfederate-green-admin.eks` to the
admin ALB address. Use Full (strict), cache bypass and native PF authentication
when proxy-only mode is enabled. The `*.eks.darkedges.com` certificate covers
these names. These routes provide admin access to the standby colour as well;
they do not place its engines into public routing.

Each colour now uses its fixed admin hostname as `pf.admin.hostname` and
`pf.admin.baseurl`, so login/logout redirects remain on that colour. The shared
admin hostname is an entry point to the active console and may redirect to its
fixed hostname. Export/commit/sync updated environment ConfigMaps and restart
admin StatefulSets to adopt canonical URL changes; engine URLs remain shared.
