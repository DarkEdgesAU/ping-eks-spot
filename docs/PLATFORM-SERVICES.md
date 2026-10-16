# Monitoring, Delegated Admin and PingCentral

These are optional POC applications prepared locally. AWS remains shut down.
Rendering is not proof of a licensed EKS deployment or of a complete login flow.
The existing Directory topology and operator-controlled PF/PA/PD routing stay
unchanged. Argo applications have manual sync and no cascading deletion finalizer.

## Names and layout

| Application | Namespace | Public hostname |
|---|---|---|
| Grafana, Prometheus, Alertmanager | monitoring | monitoring.eks.darkedges.com (Grafana only) |
| Delegated Admin 5.1.0 | identity-delegator | ext-pingdelegator.eks.darkedges.com |
| PingCentral 3.2.0 | identity-central | pingcentral-admin.eks.darkedges.com |

These prefixes follow the [P1AS network guide](https://docs.pingidentity.com/pingoneadvancedservices/2.2/network_guide/p1as_network_guide.html).
PingCentral and Delegator images are pinned by digest to the inspected `2609`
images. Delegator is a stateless UI. PingCentral uses one retained 10Gi PVC for
its complete `/opt/out`, including its H2 database and host key. H2 is appropriate
only for this demonstration; select an external supported database before a
production deployment. See [PingCentral storage guidance](https://docs.pingidentity.com/pingcentral/3.2/pingcentral_for_iam_administrators/pingcentral_installing_configuring/pingcentral_upgrading/pingcentral_upgrading_pc.html).

## Prepare without AWS

PowerShell, from the repository root:

```powershell
Copy-Item platform-services/config.example.json platform-services.json
make platform-prepare
make platform-render
make validate
```

Do not repeat `Copy-Item` over an existing configuration. All three services start
disabled (zero replicas in exported manifests). Runtime passwords, TLS keys and
license files belong under `.secrets/`, never Git. Preparation reuses the existing
CA key; restore that CA before this command if the workstation has no copy.

Local metrics/alert smoke test, with automatic container shutdown:

```powershell
py -3 scripts/platform_smoke.py
```

To include licensed PingCentral bootstrap, set `PING_IDENTITY_DEVOPS_USER` and
`PING_IDENTITY_DEVOPS_KEY` in your process environment, then:

```powershell
py -3 scripts/platform_smoke.py --central
Get-Content build/platform-smoke.json
```

The credentials are inherited by Docker, not embedded in Compose or GitOps files.
The image retrieves an evaluation license using the supported DevOps mechanism.
Local test volumes remain for inspection/restart; this test does not touch EKS.
`PLATFORM_POC_SUBNET` can override the default Docker subnet `10.234.0.0/24`.

## Next EKS startup: monitoring first

Restore the stack with the existing private teardown backup instructions. Wait
for Directory, Proxy, PF and PA to be ready. Then, from the repository root:

```powershell
make metrics-enable
make central-enable
make delegator-enable
make platform-secrets
make gitops-export
make validate
```

`platform-secrets` requires the expected kube context and installs runtime Secrets
out of band. Supply `.secrets/PingCentral.lic` or the two DevOps environment variables
for PingCentral. The license downloaded at startup is retained on its PVC.
Rotate/remove the Kubernetes `pingcentral-devops` Secret when it is no longer
needed; retained licensed instances do not need to fetch a license on every start.

Review the generated changes, then commit/merge them to the configured Argo source
revision before registering/syncing. Export alone does not deploy local changes.

```powershell
make argocd-applications
argocd app sync metrics
argocd app sync pd-blue pd-green pd-proxy pf-blue pf-green pa-blue pa-green
kubectl -n monitoring get pods,pvc
kubectl -n monitoring port-forward service/grafana 13000:3000
```

Open `https://localhost:13000`, trust the project CA, and sign in as `admin` using
`.secrets/platform/grafana-password`. Open the **Ping platform POC** dashboard.
Prometheus discovers exporters using read/list/watch access to Pods in the seven
Ping namespaces. It has no Secret-read permissions or cluster-wide role.
Sidecar metrics are served with CA-signed HTTPS. Grafana-to-Prometheus and
Prometheus-to-Alertmanager links currently use private ClusterIP HTTP; they have
no public ingress. PF/PA loopback probes accept their existing self-signed TLS
listeners and carry no credentials. These are explicit POC TLS limitations.

The initial dashboard shows exporter availability, health status and response
time, and firing alerts. PF/PA engine heartbeat JSON provides runtime statistics.
Directory and Proxy performance statistics require the new `PlatformStatsD`
endpoint. The baked profiles configure it for fresh instances. Restored PVCs
need the same commands applied explicitly; changing a profile alone does not
reconfigure an existing server. Run the commands in each product's
`pd.profile/dsconfig/60-metrics.dsconfig`, using authenticated `dsconfig` as below.
Publishing updated images is an explicit later action; no images were published
as part of local preparation.

Alerts fire after two minutes for unreachable exporters, failed/slow health
requests, missing targets and fewer than the expected 25 core Ping targets.
Adjust the count if you intentionally change non-Directory workloads. Alertmanager
has a **local-only receiver**: no email, Slack or SNS notifications are sent.
Port-forward Prometheus to inspect **Alerts**:

```powershell
kubectl -n monitoring port-forward service/prometheus 19090:9090
```

Open `http://localhost:19090`. Verify 25 targets, generate a known test request,
and confirm the corresponding product statistics change. Heartbeat success does
not establish successful authentication, replication or data durability.

## Delegator integration

```powershell
argocd app sync delegator
kubectl -n identity-delegator get pods
```

Its public ingress is gated by `delegator_integration_verified`. The UI uses
generic OIDC discovery with authorization code + PKCE; no client secret or LDAP
admin credentials enter browser JavaScript. API calls use the same origin's
`/dadmin/` path, reverse-proxied with TLS verification to Directory. The configured
Directory colour is explicit in `platform-services.json`; it is independent of
the LDAPS Proxy route. Change `directory_colour`, export and sync Delegator when
moving this API backend. A profile checksum changes the pod template, so this
sync rolls the Delegator pods and reloads NGINX from the updated profile.
Do not infer an HTTP API cutover from an LDAPS promotion.

On the next live startup:

1. Export current PF and Directory configuration before making changes. Check
   the selected Directory HTTPS service certificate covers its service DNS name.
2. In **both PF colours**, create the `dadmin` public client using the generated
   `build/delegator-pf-client.json` as the field reference. Use authorization code,
   PKCE required, no client authentication, OIDC policy `pingdelegator`, ATM `jwt`,
   and the exact redirect URLs in that file. Restrict common scopes to `openid`
   and explicitly allow the exclusive scope
   `urn:pingidentity:directory-delegated-admin`. Add the exact Delegator HTTPS
   origin to PF's OAuth CORS allowed origins.
   Preserve all existing origins/settings; do not replace the authorization server
   settings object with a partial payload.
3. Confirm the existing HTML form authentication policy can authenticate the
   seeded Directory administrator. Ensure the access token's `Username` claim
   is the Directory `entryUUID`, not its human-readable uid. Verify the actual
   token's algorithm and claims; the supplied validator expects RS256.
4. Export the **JWT access token signing public certificate** from the selected
   PF ATM as `build/delegator-signing.crt`. This is not the HTTPS certificate.
   Both PF colours must use the trusted signing key, or add both public signing
   certificates to the validator before testing a PF cutover.
5. Fresh Directory instances now receive the common resource/permission configuration
   from the baked `50-delegated-admin.dsconfig`. Apply only
   `build/delegator-signing.dsconfig` to those instances. For retained instances
   that predate this profile, review and apply the combined `build/delegator.dsconfig`.
   The separate `build/delegator-common.dsconfig` is available for staged migration.
   Its rights grant CRUD only on the configured
   people/groups branches to `uid=administrator,ou=people,<base DN>`. Other users
   receive no delegated rights. The two global ACIs enable required controls and
   extended operations; they do not grant general entry CRUD.

For the **first application** of this configuration, PowerShell can stream the
commands to each of the six replicas. Existing object names cause an error rather
than overwriting configuration; inspect and use corresponding `set-...-prop`
commands for subsequent changes. Directory config is local to each replica even
though the resulting data ACIs replicate.

```powershell
$connection = @'
dsconfig() {
  command dsconfig --noPropertiesFile --hostname localhost --port 1636 --useSSL \
    --trustStorePath /run/ping-secrets/truststore.p12 \
    --trustStorePasswordFile /run/ping-secrets/truststore.pin \
    --bindDN cn=administrator --bindPasswordFile /run/ping-secrets/root-user-password \
    --no-prompt "$@"
}
'@
foreach ($colour in @('blue','green')) {
  foreach ($replica in 0..2) {
    $pod = "pd-pingdirectory-$replica"
    kubectl -n "identity-$colour" cp build/delegator-signing.crt "${pod}:/tmp/delegator-signing.crt" -c pingdirectory
    if ($LASTEXITCODE -ne 0) { throw 'Certificate copy failed' }
    ("set -eu`n" + $connection + "`n" + (Get-Content build/delegator.dsconfig -Raw)) |
      kubectl -n "identity-$colour" exec -i $pod -c pingdirectory -- sh -s
    if ($LASTEXITCODE -ne 0) { throw "Configuration failed on $colour/$pod" }
  }
}
```

Repeat the same connection wrapper with the StatsD configuration files for restored
Directory/Proxy pods. Do not run the Directory Stats Collector plugin command on
Proxy; its supplied file intentionally omits that command.

For freshly initialized instances, substitute `build/delegator-signing.dsconfig`
in that loop; the common configuration is already installed. The `dadmin` LDAP
seed is deliberately disabled until PF's supported API/UI configures required
PKCE and enables the client. Do not use direct supplemental-info edits to guess
how a PF version serializes PKCE. All other upstream client definitions are preserved,
with runtime-generated credentials and replacement JWKs instead of the demo values;
the original upstream file is preserved privately under
`.secrets/profile-backups/pingdirectory/` in this workspace.

Before installing Directory runtime secrets, run:

```powershell
make oauth-client-secrets
make secrets
```

The first command is local only and uses the configured PF `2609` image's hashing
library. It generates unique secrets for five confidential clients and new keys
for the three clients with embedded JWKS. Existing generated secrets/keys are
preserved on reruns. `profiles/pingdirectory/oauth-clients.json` maps each client
ID to its files under `.secrets/oauth-clients/`; `.secret` files contain the values
to configure in your test applications, and `.private-jwks.json` contains their
private signing/decryption keys. Only hashes, public JWKS and the HS256 client's
required obfuscated secret enter the Directory runtime Secret. Obfuscation is
reversible and is not encryption at rest; keep that value private as well.
The generator verifies hashes and obfuscation using the installed PF libraries.
It does not start PF or publish an image.

For an already populated Directory, new profile LDIF does not update existing
entries. Update each of the five clients through PF's supported Admin API/UI using
its new `.secret` value, and replace the three JWKS values with the generated public
keys. PF then updates its LDAP client store using the deployed version's format.
This also saves `ac_oic_client`'s required HS256 reversible value correctly with
the running PF encryption keys. Coordinate this change with the test applications
and verify both colours can use the configured secrets/keys. Do not reimport the
whole seed file over existing data. Preparation does not change live LDAP entries.

New Directory keystores include both colour service DNS names. Existing keystores
are never automatically overwritten by `make secrets`; verify the current
certificate has `pd-pingdirectory.identity-blue.svc.cluster.local` and the green
equivalent before using the same-origin API proxy. If absent, renew the listener
certificate using the existing CA and preserve its alias/PIN before reinstalling
runtime Secrets and restarting replicas one at a time with replication checks.
Retain the previous key stores for rollback. Do not regenerate the CA or delete
PVCs to address a certificate-name mismatch.

Create the Cloudflare proxied CNAME after the ingress is created. For initial
browser login testing you can create the ingress only after reviewing the above
integration and setting `delegator_integration_verified=true` locally, then run
`make platform-ingress`. Record successful administrator login, search, disposable
user create/update/delete, and an ordinary user's denied access. The boolean is an
operator gate, not a claim that these checks have run automatically. Keep public
ingress disabled if the API or authorization configuration remains incomplete.

Reference: [OIDC client setup](https://docs.pingidentity.com/pingdirectory/10.3/delegated_admin_application_guide/pd_da_config_oidc_idp.html)
and [signed-token validation](https://docs.pingidentity.com/pingdirectory/10.2/managing_access_control/pd_ds_handle_signed_tokens.html).

## PingCentral integration

```powershell
argocd app sync pingcentral
kubectl -n identity-central get pods,pvc
kubectl -n identity-central port-forward service/pingcentral 19022:9022
```

Open `https://localhost:19022/pass/login`, complete licensed
bootstrap and **change the initial Administrator password** before public access.
The image's initial login is version-dependent: use the PingCentral 3.2 installation
documentation if needed. Store the replacement in your password manager.

PingCentral initially generates its own self-signed certificate in its database;
Spring `server.ssl.key-store` properties do not replace this native key pair.
For first local/port-forward access, verify that bootstrap certificate. In
**Security > Server TLS Key Pair > Import Key Pair**, upload
`.secrets/platform/keystore.p12`, using `.secrets/platform/keystore.pin` as the
password and `pingcentral` as the alias. Restart PingCentral and verify the
listener using the project CA before enabling public ingress. See
[native certificate import](https://docs.pingidentity.com/pingcentral/3.1.1/pingcentral_for_iam_administrators/pingcentral_installing_configuring/pingcentral_admin_ssl_cert.html).
The local smoke test pins the initial certificate solely for its loopback health
request; it does not claim the native project-CA import has been completed.

In PingCentral, add two explicit environments, `lab-blue` and `lab-green`.
`build/central-environments.json` lists the private admin URLs. It is a setup
inventory, not an API import payload. These are deployment colours, not separate
development and production data environments.

Import the actual PF/PA admin issuing certificates into PingCentral's trusted
certificates and verify hostname matching. The current PF/PA listeners may still
use their stock certificates; the project CA alone does not make those trusted.
Use native API authentication for this lab if still enabled, or configure OAuth
client credentials using [the supported PF/PA integration](https://docs.pingidentity.com/pingcentral/3.2/pingcentral_for_iam_administrators/pingcentral_installing_configuring/pingcentral_pf_pa_sso.html).
Do not disable certificate validation to bypass setup failures. PF green API
credentials must be rechecked after restore.

Verify both environments, create one OIDC template from an existing test client,
onboard a disposable application, and confirm it appears only in its selected
environment. PingCentral application promotion must not change the platform's
active blue/green route. Test restart persistence before calling onboarding ready.

After changing the password and verifying the imported listener certificate,
set `central_password_changed=true` and `central_tls_verified=true`, then:

```powershell
make platform-ingress
kubectl -n monitoring get ingress
kubectl -n identity-delegator get ingress
kubectl -n identity-central get ingress
```

Use the reported ALB DNS names for the respective proxied Cloudflare CNAMEs.
Ingress uses the existing ACM certificate and Cloudflare source CIDRs from
`gitops/config.json`, with HTTPS to each application. Prometheus and Alertmanager
remain private. Public URLs do not exist while AWS is shut down.

## Remaining live acceptance

- EKS scrape discovery, all expected targets and actual Directory metrics.
- Verified Delegator login, writes, denied unauthorized access and PF key continuity.
- PingCentral PF/PA TLS/authentication, template onboarding and retained-state restart.
- Public ALB/Cloudflare health and final browser redirects on the three hostnames.
- Capture all deployed namespaces at teardown, including monitoring,
  identity-central, logging and both PA colours. The recovery helper includes
  their retained PVCs/secrets when inventoried and refuses to skip a namespace
  inventory if the saved PV list contains a claim belonging to it. Historical
  backups can omit services that had not yet been deployed.

The supplied alerts and dashboard are POC coverage, not the complete hosted P1AS
dashboard catalog or its managed alerting service. Retention remains a later choice.
