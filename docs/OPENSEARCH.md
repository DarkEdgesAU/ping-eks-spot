# Minimal OpenSearch log-shipping POC

This first version proves delivery. It uses one OpenSearch node, Dashboards,
Fluent Bit file sidecars on every Ping role/colour, and a node DaemonSet for
container stdout/stderr. Retention policies, archival, parsing native audit
schemas and guarantees against log loss are deferred. No S3/SQS pipeline or
managed OpenSearch domain is created. The existing internal CA signs the new
listeners; authentication and TLS remain enabled.

## Prove shipping locally

From the repository root, with Docker Desktop running:

```powershell
py -3 -m pip install -r requirements.txt
make logging-local-test
# Or leave the local UI running after the checks:
py -3 scripts/logging_smoke.py --keep
```

The test writes synthetic server/audit files, emits a simulated CRI container
log, waits for indexed markers, restarts the file collector and verifies a new
marker. It checks authenticated HTTPS, rejects anonymous OpenSearch requests,
checks the Dashboards page and creates `logs-ping-*` / `logs-containers-*` views.
Evidence is `build/logging-smoke.json`. These are real containers and actual
shipping, but synthetic events: this does not prove native Ping audit coverage.

Local UI: `https://localhost:15601/`. Username: `admin`. Password file:
`.secrets/logging/admin-password`. The browser needs to trust the development
CA to avoid certificate warnings; no security checks are disabled in the test.
Private generated files, including Docker Compose credentials, stay under
`.secrets/logging/`. A logging-only copy of the existing CA certificate adds
modern certificate extensions using the same subject/key/serial; it does not
replace the shared CA or other services' trust stores.

Stop only these local containers:

```powershell
docker compose -f .secrets/logging/compose.yaml down
```

Volumes remain for subsequent tests. Docker's test network uses `10.233.0.0/24`;
override `LOGGING_POC_SUBNET` if that conflicts with your network. The test never
prunes unrelated Docker networks or volumes.

## Deploy after restoring the AWS stack

AWS was torn down on 16 October. Use that night's private `RESTORE.md` first;
do not generate replacement Directory/PF/PA secrets or claims. Infrastructure
recreation resumes AWS charges. This POC needs the existing EBS CSI driver,
`pingdirectory-ebs` Retain StorageClass and ALB controller.

```powershell
make logging-enable
make logging-secrets
make gitops-export
make validate
```

`logging-enable` creates ignored `observability.json` with collection enabled.
Copy `gitops/config.example.json` to `gitops/config.json` if you do not already
have that local file; preserve the existing ACM ARN and Cloudflare ranges.
All secrets remain runtime-only; the rendered manifests reference them by name.
Review, commit and push the generated manifests and merge the implementation
before syncing: `gitops/config.json` now targets `main`. Register the updated
namespace-scoped Argo projects and applications:

```powershell
make argocd-applications
```

Manually sync `logging` first and wait for its OpenSearch and Dashboards pods.
Then sync Ping candidates/roles deliberately: adding a sidecar changes the pod
template and can restart workloads. Preserve Directory topology, existing PVCs,
replica counts and routing. Start with an inactive candidate and check its logs
before rolling other replicas; do not prune retained resources.

```powershell
kubectl -n logging get pods,pvc
kubectl -n logging rollout status statefulset/opensearch --timeout=300s
kubectl -n logging rollout status deployment/dashboards --timeout=300s
make logging-verify
make logging-ingress
kubectl -n logging get ingress logs -o wide
```

`logging-verify` writes a uniquely tagged `logging-poc.log` file in each live
Ping pod and confirms the per-pod events and namespace metadata in OpenSearch.
It records `build/logging-live-report.json`. This is an explicit test write,
not a native authentication/audit transaction. The default POC buffer is bounded
and ephemeral; repeated delivery is retried but node/pod loss can lose data.

Create a proxied Cloudflare CNAME `logs.eks.darkedges.com` pointing to the new
Ingress ADDRESS. Use Full (strict) and bypass caching. The public ALB exposes
only HTTPS Dashboards; search REST and transport ports remain private. Origin
ACM and Cloudflare CIDRs come from `gitops/config.json`. Native OpenSearch login
protects the UI; no Cloudflare Access or PF SSO setup is required for this POC.
The readiness/ALB check uses `/app/login`; `/api/status` requires authentication
and would incorrectly make the backend appear unhealthy without credentials.

The logging application includes a bounded Argo CD PostSync Job that creates
`logs-ping-*` and `logs-containers-*` with `@timestamp` in the shared **Global**
tenant after Dashboards is healthy. It reuses existing patterns (including manual
UUIDs), preserves existing defaults, and otherwise selects Ping logs and Discover
as the starting view. Choose the Global tenant in Dashboards to see shared views;
private tenants keep their own saved objects. Browser onboarding prompts may still
be shown; Discover has the patterns ready without adding data or an agent.

For the existing stack, after merging these manifests, deliver the existing admin
password to the dedicated bootstrap Secret, register the updated namespace-scoped
Job permission, and sync logging:

```powershell
make logging-bootstrap-secrets 'PYTHON=py -3'
make argocd-applications 'PYTHON=py -3'
argocd app sync logging --grpc-web
argocd app wait logging --sync --health --operation --timeout 600 --grpc-web
```

Use the EKS kubeconfig and AWS profile for these commands. `bootstrap-secrets`
reads `.secrets/logging/admin-password`; it never generates credentials. On a
lifecycle restore the bootstrap Secret is reconstructed automatically from the
version-pinned Vault/encrypted backup files before sync. Credentials stay out of
Git. The Job mounts only the CA certificate from the TLS Secret, validates HTTPS,
and runs without a Kubernetes API token. Failed Jobs remain available for
diagnostics; successful Jobs are removed and recreated on subsequent syncs.

Use Discover to inspect `log`, product, colour, namespace/pod, source file and node
fields. Raw product JSON/stack traces are retained as the `log` message for this POC.
The bootstrap uses the documented [Dashboards Saved Objects APIs](https://docs.opensearch.org/latest/dashboards/management/saved-objects-api/).

## Boundaries and operating notes

- Existing Directory/admin `/opt/out` storage is reused read-only by the
  collector. Ephemeral engines/Proxy receive a shared emptyDir `/opt/out` so
  application files are visible to their sidecar. Live bootstrap acceptance is
  required for those new shared volumes before promotion.
- The collector tails explicit log globs, but its read-only volume exposes the
  wider instance directory. Narrow mounts and isolation are future hardening.
  The collector does not mount `/run/ping-secrets`; its credential permits
  access to `logs-*`, not security configuration. Do not enable credential-heavy
  HAR/TRACE logging merely to generate events.
- Only logs actually produced can be shipped. Native audit publishers/appenders
  must be enabled separately and tested using real binds, writes, logins and
  administrative changes. This implementation does not silently enable LDIF
  data-change logging or detailed PF configuration diffs.
- This covers Ping files and node container logs. Managed EKS control-plane
  audit remains in CloudWatch; ALB access/CloudTrail ingestion is future work.
  File/stdout copies can produce duplicates; no exactly-once claim is made.
- A single OpenSearch node is not HA; default replica shards may leave health
  yellow. The 20Gi search PVC is Retain and must be included in later teardown
  inventories along with logging Secrets and CA-issued keys. Do not delete it
  as a side effect of application pruning. Log volume growth is unbounded until
  a retention/cleanup policy is explicitly added; monitor free disk during POC.
- Images are pinned to OpenSearch/Dashboards 3.6.0 and Fluent Bit 4.2.3. The
  project `make validate` runs mount, namespace, secret-reference and TLS tests;
  the Docker smoke test is the delivery acceptance check.
