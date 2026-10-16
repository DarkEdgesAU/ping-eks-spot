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
