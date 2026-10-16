# OpenSearch logging and audit POC proposal

Implementation decision: the user accepted possible POC data loss and deferred
retention. The initial implementation therefore ships directly to a single-node
EKS OpenSearch deployment, with no S3/SQS/Data Prepper layer. See
[OPENSEARCH.md](OPENSEARCH.md). The remainder is the earlier investigation and
future recovery design, not the requirements for the simplified POC.

Investigated 16 October 2026. Planning only: no infrastructure was recreated,
images published, log publishers changed or workloads deployed.

Confirmed: UI hostname `logs.eks.darkedges.com`; compare AWS-managed and
EKS-hosted OpenSearch; one day for the POC. Provisionally interpret this as
24 hours of searchable events. Archive retention, permitted log loss, UI
authentication and availability during overnight teardown remain decisions.

## Recommendation

Use Fluent Bit sidecars for Ping file logs, a Fluent Bit DaemonSet for node and
container logs, and an AWS-native path for control-plane and load-balancer logs.
Prefer an S3-first pipeline: collectors upload normalized NDJSON; S3 object
notifications enter SQS; Data Prepper or OpenSearch Ingestion reads objects and
indexes them with end-to-end acknowledgements. S3 is the replay source; the
search index is rebuildable. Avoid two independent archive/search outputs as
the primary delivery guarantee.

AWS documents this Fluent Bit/S3/SQS collection pattern specifically for
[PF JSON audit logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/pingidentity-pingfederate-source-setup.html).
Use that as a collection reference, not as a requirement to deploy CloudWatch's
product-specific pipeline or copy its example IAM policy unchanged.

| Choice | POC fit | Recovery and operating tradeoff |
|---|---|---|
| OpenSearch on EKS + Data Prepper | Provisional recommendation if the UI may go offline overnight | Argo-managed; retained EBS and S3 replay; operators maintain search nodes, certificates, snapshots and upgrades |
| AWS-managed OpenSearch + OpenSearch Ingestion | Recommend for a lasting service or an independently available UI | Managed search operations and ingestion; provisioned capacity continues billing until removed; requires a deliberate independent network/state lifecycle |
| OpenSearch Serverless | Evaluate only if ongoing scale warrants it | Different capacity/security lifecycle; do not assume a one-day lab is automatically cheaper |

For the smallest POC, a single search node on On-Demand capacity with retained
EBS demonstrates restart/restore, but has downtime on node or AZ failure.
For availability testing, use three nodes across three AZs with appropriate
replicas and On-Demand capacity. Neither layout proves delivery from applications.
The [official Helm deployment](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/helm/)
defaults to three nodes; size from measured event rate, bytes and buffer backlog,
not just the 24-hour retention target. No exact spend estimate is established.
[Managed-domain billing](https://aws.amazon.com/opensearch-service/pricing/)
continues until instances terminate; this affects overnight teardown decisions.

## Coverage inventory

Paths below are expected container paths; verify actual files, enabled appenders,
formats and rotation against the restored image 2609 before implementing.

| Source | Streams to collect | Collection |
|---|---|---|
| Directory, both colours and every replica | `/opt/out/instance/logs/`: errors, JSON LDAP access, configuration audit, replication/startup, optional HTTP access and change audit | Sidecar reads log subdirectory; existing per-replica retained PVC |
| Directory Proxy, all replicas | Errors, LDAP request/result access, config audit, startup and routing changes | Sidecar; currently ephemeral source storage |
| PF admins and engines, both colours | `/opt/out/instance/log/`: server, audit, admin, admin-api, runtime-api, request and transaction logs where enabled | Sidecar; admins retained, engines ephemeral |
| PA admins and engines, both colours | `/opt/out/instance/log/`: pingaccess, engine audit, API audit, agent/sideband streams if enabled | Sidecar; admins retained, engines ephemeral |
| Argo CD, ALB controller, EBS CSI, CNI, DNS and other Kubernetes containers | stdout/stderr, including startup/init failures | Node DaemonSet with Kubernetes metadata |
| Kubernetes events | Scheduling, eviction, attach and probe events | Event collector; events are not an audit substitute |
| EKS control plane | API, audit, authenticator, controller manager, scheduler | Existing CloudWatch log group -> archive/ingestion bridge; sidecars cannot read managed control-plane files |
| ALB | Access and supported connection logs | Enable delivery to S3, use format-specific ingestion |
| AWS control-plane activity | Relevant CloudTrail management events | Optional explicit trail/source integration; separate from Kubernetes audit |

Current repository observations:

- `terraform/eks.tf` enables all five EKS control-plane streams with 30-day
  CloudWatch retention. Teardown currently deletes that log group: archive and
  drain it before deletion or move its lifecycle into retained observability.
- Directory has six retained data PVCs; PF/PA have four retained admin PVCs.
  Proxy and PF/PA engine logs do not currently survive pod replacement.
- `10-log-publishers.dsconfig` disables Data Recovery Log. Its comment mentions
  audit, but this does not establish whether access/config/change audit is enabled.
- PF sets `pf.log.eventdetail=false`. Treat enabling detailed configuration diffs
  as a separate decision because they can contain sensitive values.
- Chart 0.16.0 exposes `includeSidecars` and `sidecars` in its workload template.
  Existing ephemeral pods will need an explicit shared log volume; containers
  do not automatically share their writable filesystem.

Prefer supported JSON audit/access appenders. Preserve source event IDs and
correlation IDs; use explicit parsers for legacy text, multiline Java stack traces
and Directory LDIF change records. Do not treat LDIF line-by-line as independent
events. Confirm coverage by generating known successful/failed binds, writes,
logins and administrative changes on every role and colour.
References: [Directory logger types](https://prod-docs.pingidentity.com/pingdirectory/11.1/pingdirectory_security_guide/pd_sec_types_loggers.html),
[PF log inventory](https://docs.pingidentity.com/pingfederate/13.0/administrators_reference_guide/pf_log_files.html),
[PA logging types](https://docs.pingidentity.com/pingaccess/9.1/configuring_and_customizing_pingaccess/pa_types_of_logging.html).

## Delivery and failure boundaries

1. Mount only each product's log directory read-only into the sidecar. Keep its
   checkpoint database and output spool in a separate writable subdirectory.
   Do not expose the whole product instance, licenses, master keys or passwords.
2. Persist both file offsets and queued payloads. Configure appropriate fsync,
   checksums, bounded storage, rotation handling and retries for the pinned
   collector version. A checkpoint alone is not an acknowledged-delivery ledger.
3. Fluent Bit's S3 output has its own `store_dir`, limits and retry semantics;
   generic output queue settings and success metrics do not establish S3 delivery.
   Prefer short completed PutObject batches for the POC, unique object keys per
   pod/stream, and ingestion-lag measurements. Multipart parts are not yet a
   completed replayable object. See [S3 output](https://docs.fluentbit.io/manual/data-pipeline/outputs/s3).
4. S3/SQS ingestion acknowledges notifications after sink completion. Use a DLQ
   for poison records, monitor queue age and implement explicit redrive and
   S3 object replay. Deduplicate using a stable event identity or S3 object key
   plus record ordinal, not the message text alone. Preserve distinct identical
   legitimate events. See [S3 source acknowledgements](https://docs.opensearch.org/latest/data-prepper/pipelines/configuration/sources/s3/).
5. For existing retained pods, store checkpoints/spool on persistent storage
   without allowing logs to consume the directory database's free space.
   Ephemeral engine/Proxy pods require either a measured loss allowance or
   retained source log/spool storage with stable recovery identity. A dedicated
   per-instance logging PVC/StatefulSet design is an option needing a separate
   workload compatibility review. A central queue cannot recover an event that
   never left the failed source pod.
6. AZ-bound EBS can survive node loss but cannot instantly move to another AZ.
   For an AZ outage, already uploaded S3 records remain replayable; outstanding
   source buffers may wait for their AZ. Do not claim zero loss or exactly-once
   delivery across the entire chain.
7. Prefer Kubernetes native sidecar lifecycle for startup/shutdown ordering,
   subject to chart rendering verification. Existing regular sidecar extension
   points alone do not guarantee that the collector outlives the application.
   See [native sidecars](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/).

Define buffer capacity from peak bytes/second x maximum delivery outage x safety
factor. Alert before exhaustion; never silently accept oldest-record eviction
as audit recovery. Decide whether full audit storage should block application
activity or raise an explicit loss incident. Retries cannot fix a full source disk.

## UI, schema and retention

For EKS hosting: Cloudflare proxied DNS -> public ALB on 443 -> OpenSearch
Dashboards over verified internal TLS. Restrict ALB sources to current Cloudflare
ranges, use ACM at the origin and the internal CA within the cluster. Search
REST and transport ports stay private. Native named users/read-only roles are
the simplest POC option; PF SSO requires independent emergency access when PF
is unavailable. Cloudflare proxying is not application authentication.

For a managed VPC domain, design and test a supported proxy/custom-endpoint and
authentication path; do not assume a CNAME or an ALB target directly solves VPC
Dashboards access. See [AWS Dashboards access](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/dashboards.html).

Separate operational, access/security, configuration audit and platform datasets.
Include UTC event/ingest timestamps, cluster, namespace, colour, product, role,
pod UID, node/AZ, image, stream, severity and source correlation identifiers.
Avoid per-pod indices and unrestricted dynamic mappings. Remove credentials,
authorization headers, cookies and tokens before remote storage; limit LDAP
attribute values, query strings and configuration diffs. HAR/TRACE and binary
heap dumps are not normal searchable logs.

Propose 24-hour searchable retention via tested rollover/ISM. Index-based
expiration is not exact per-event deletion at 24 hours; document maximum overrun.
Keep raw S3 data for longer than the maximum replay outage, pending an explicit
archive-retention choice. Do not expire raw objects before ingestion/recovery
can finish. S3 lifecycle expiry is asynchronous; versioned objects need an
explicit noncurrent-version policy. Do not enable irreversible audit retention
locks or legal holds for this POC without an explicit requirement.

## Implementation sequence and acceptance

1. Resolve the remaining availability/authentication/loss choices and confirm
   whether one day means searchable retention, POC runtime, or both. Measure
   log volume under the existing bind/search/write cutover test.
2. Add an isolated observability AWS stack/state for retained S3, SQS, IAM/KMS
   and any managed service. Keep AWS infrastructure and Kubernetes bootstrap
   separate. Add Argo applications for collectors, ingestion, search and UI.
3. Pin images/charts, add profile log layouts and sidecars, explicitly share log
   directories, then validate rendered mounts, permissions and resource budgets.
   Do not change Directory replica count or replication/routing behavior.
4. Set up templates, parsers, retention, dashboards and role mappings. Add
   ingest-lag, buffer-space, dropped-event, DLQ-age and search health alerts.
   Test redaction against secrets-shaped synthetic values before using real logs.
5. Test sidecar crash, restart while rotating files, pod replacement, abrupt Spot
   node loss, OpenSearch outage/429, S3 outage, credential expiry, full buffers,
   malformed records, duplicate SQS delivery, index deletion/rebuild and AZ loss.
   Inject numbered events and reconcile source -> S3 -> search with explicit
   counts of missing, duplicate and rejected events and recovery time.
6. Extend overnight teardown: stop new requests, stop producers, wait for file
   drain and completed S3 objects, verify lag/queue state, snapshot retained
   volumes, preserve replay manifests, then remove disposable compute. Include
   observability resources in recovery inventory and document remaining costs.

Success means all agreed streams are observed, outages recover within measured
limits, replay is demonstrated and any loss window is reported. Pod readiness,
an HTTP 200 from ingestion, a local PVC or a successful snapshot alone does not
prove audit completeness.
