# PingAccess blue/green

Two independent PingAccess clusters use `identity-access-blue` and
`identity-access-green`. Each has one StatefulSet admin with a retained 10Gi EBS
claim and three Deployment engines spread across three AZs and hosts. Engines
join only their own colour's admin through the stock DevOps registration hooks.
This is separate from Directory replication and the PF topology.

The minimal baked profile uses the official stock initialization workflow, not
a demo database or a bulk import. Runtime Secrets provide the license and an
independent generated admin password per colour. Restart hooks preserve the
database, console changes and encryption keys. Back up each admin database,
`pa.jwk`, license and configuration export before upgrades or retirement.

## Prepare locally

```powershell
Copy-Item access.example.json access.json
make pa-render TARGET=blue
make pa-render TARGET=green
make gitops-export
make validate
make terraform-check
make plan
```

Review the infrastructure plan: this change adds a PingAccess ECR repository;
it does not deploy PA through the Terraform AWS stack. Apply the reviewed plan
when ready. Supply an authorized license at `.secrets/PingAccess.lic` and retain
the existing shared internal CA. Never put licenses, passwords, exported
configuration, databases or master/private keys in Git.

```powershell
make pa-secrets
make pa-build-image TARGET=blue
# Same initial image tag is shared: do not push pa-v1 twice to immutable ECR.
make gitops-export
```

Review/commit/push the secret-free `pa-blue` and `pa-green` exports and application
definitions, then register them with `make argocd-applications`. Manually sync
each PA application without pruning. Argo CD creates workloads; `pa-secrets`
creates the namespaces and runtime Secrets first. Existing applications retain
their manual sync behavior. Nothing is deployed by render/export/validate.

## Ingress

Set `origin_certificate_arn` and `cloudflare_ipv4_cidrs` in `access.json`.
Proxy-only is explicit and uses native PA authentication. Configure the ACM
certificate and Cloudflare edge certificate to cover these draft hostnames:

| Host | Route |
|---|---|
| `pingaccess-admin.eks.darkedges.com` | Active admin |
| `pingaccess-blue-admin.eks.darkedges.com` | Fixed blue admin |
| `pingaccess-green-admin.eks.darkedges.com` | Fixed green admin |
| `access.eks.darkedges.com` | Active engines |

Run `make pa-ingress` after the clusters are healthy, then get the ALB addresses
with `kubectl get ingress -A -o wide`. PA has its own shared admin ALB and engine
ALB; it does not reuse PF's ALB groups. Add proxied Cloudflare CNAMEs, Full
(strict) TLS and cache bypass. Only 443 is public; admin 9000, engine 3000 and
configuration-query 9090 remain internal service ports. Admin credentials are
`administrator` and the respective `.secrets/pa-COLOUR/admin-password` file.

Ingress commands preserve the observed active colour, defaulting to blue only
when no PA ingress exists. Fixed colour admin routes remain available during
promotion; standby shared engine routes return 503.

## Configuration and acceptance

### Front PingFederate at /identity/

`scripts/access_config.py` configures the native PingFederate runtime proxy.
Each PA colour targets its matching PF engine Service on internal HTTPS 9031.
The public virtual host is `access.eks.darkedges.com:443` and context root is
`/identity` (no trailing slash in configuration). PF handles authentication;
no PA web session is required to reach PF login endpoints.

Set `identity_context_root` to `/identity` in `access.json` and set
`public_base_url` to `https://access.eks.darkedges.com/identity` in `federate.json`.
The latter prevents later PF configuration Terraform runs reverting the URL.
Existing federation entity IDs are preserved by this integration command.

```powershell
make pa-identity-plan TARGET=blue
make pa-identity-apply TARGET=blue
make pa-identity-verify TARGET=blue
make pa-identity-plan TARGET=green
make pa-identity-apply TARGET=green
make pa-identity-verify TARGET=green
```

Plan reads the current APIs without sending mutations. Apply exports both
configurations beneath `.secrets/identity-backups/` before making individual
API updates and requesting PF cluster replication. It does not bulk-import
configuration or change active colours. Rerunning adopts named virtual hosts,
certificates and trust groups rather than creating duplicates. Treat backups
as secrets; retain PA master keys separately for full disaster recovery.
The request rewrite removes `/identity` before forwarding to PF's existing
root runtime; PF's public base URL generates the prefixed links and metadata.
The verification command tunnels directly to a ready PA engine, verifies TLS,
and checks heartbeat plus the public paths in OpenID discovery metadata.

PF API credentials default to `.secrets/federate-admin-password`. If a colour
has a different password, put its current password in
`.secrets/pf-COLOUR/admin-password`. Never put the password on the command line.
An API 403 means credentials or administrator permissions need checking before
retrying. No green changes occur when its initial PF API read fails.

Current PF runtime certificates have `CN=localhost`. The command retrieves
their public certificate through authenticated Kubernetes, pins it in a PA
trust group and verifies the expected hostname `localhost`. Certificate and
date checking stay enabled. This is a development bridge, not the shared CA
listener migration. Rerun after replacing PF certificates, and update the
expected hostname to their actual SAN before adopting CA-issued listeners.

Verify `https://access.eks.darkedges.com/identity/pf/heartbeat.ping` and
`https://access.eks.darkedges.com/identity/.well-known/openid-configuration`.
The prefix alone is not a login application: exercise a real configured OIDC
or SAML client and check redirects, logout and metadata before promotion.
Clients consuming PF metadata may need to refresh their endpoint URLs.
The default OAuth issuer also follows the new base URL. Update client issuer
validation accordingly; existing tokens issued under the old issuer require
an explicit migration decision. The `/identity/` root itself can return PF's
normal 404 because no particular login transaction was requested.
Promote PF and PA as a coordinated matching-colour pair; their ingress changes
are separate and do not promise session continuity.

See [Ping's proxied runtime settings](https://docs.pingidentity.com/pingaccess/9.1/pingaccess_user_interface_reference_guide/pa_pf_runtime.html).

The profile initializes an admin and cluster. It intentionally does not ship a
working protected application, PF OAuth client, token-provider secret or demo
users. Configure matching PF OAuth clients and PA token-provider settings,
virtual hosts, sites, applications, web sessions and rules independently in
both colours before testing a real login. Do not copy exported encrypted
configuration between colours without handling the corresponding keys.

The stock image creates self-signed listener certificates and uses insecure
curl in some bootstrap/engine-registration calls. Mounting `ca.crt` does not
replace those certificates or establish CA-verified internal TLS. Before a
production cutover, import CA-signed admin/engine/CONFIG QUERY key pairs, assign
the listeners, verify SANs/trust and replace/validate the stock registration
workflow. This starter does not claim that internal TLS requirement is complete.
The `internal_tls_verified` promotion attestation must remain false until that
work is completed and verified. Licensed startup on image 2609, effective product
version, profile hooks, registration, canonical admin redirects, health checks
and application/session behavior all require live acceptance testing.

## Promotion

Prepare evidence only after verifying the target image and cluster. The JSON
requires `cluster_arn` (Terraform output), `target_colour`, `image_tag`, a current
UTC `verified_at`, `operator`, `notes`, and these boolean attestations:
`configuration_verified`, `internal_tls_verified`, `login_verified`,
`session_compatibility_verified`, `rollback_verified`. All must be true based on
actual checks, and evidence expires after five minutes.

```powershell
make pa-promote TARGET=green EVIDENCE=build/pa-green-evidence.json
make pa-status
```

Promotion requires one ready admin, three ready engines on three AZs, and the
verified image tag. ALB rule changes are sequential and target registration/drain
is asynchronous. Verify connection drain and new/existing sessions; separate
clusters do not establish automatic session continuity. Keep both admin PVCs
and original secrets for rollback. Retirement/deletion is a separate operation.

References: [Ping profile structure](https://developer.pingidentity.com/devops/reference/profileStructures.html),
[PingAccess clustered Helm deployment](https://developer.pingidentity.com/devops/deployment/deployPACluster.html),
[PingAccess image configuration](https://developer.pingidentity.com/devops/docker-images/pingaccess/README.html).
