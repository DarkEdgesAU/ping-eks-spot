# PingOne Advanced Services comparison

Assessment date: 17 October 2026. Scope: this repository, its current uncommitted
OpenSearch POC, local acceptance reports and the previously observed EKS lab.
No services were deployed, modified or contacted as part of this assessment.
This is not a fresh live inventory or an assessment of the supplied third-party
Advanced Services tenant. The last verified AWS teardown completed on 16 October;
the local OpenSearch POC was subsequently stopped at the operator's request.

## Conclusion

The lab contains the core identity runtime families: PingDirectory/Proxy,
PingFederate and PingAccess. Argo CD, branded ALB ingress, blue/green controls,
Terraform and retained storage are implemented. OpenSearch delivery has been
proved locally using synthetic events, with EKS acceptance still pending.

The largest missing component families are PingCentral, Delegated Administration,
and Prometheus/Grafana with alerting. PingDataSync is also absent, but is relevant
only if external-directory/data-source synchronization is part of the intended
use case. Directory replica synchronization is not a DataSync deployment.

The larger remaining difference is operational: common administrator identity,
self-service workflows, continuous backups, tested failure recovery, lifecycle
automation, security maintenance and the managed-service operating model.
Deploying equivalent applications does not establish the hosted service's SLA,
support arrangements or recovery guarantees.

## Reference baseline

Ping's public Advanced Services 2.2 release documentation includes PingCentral
alongside PF, PA and the Directory suite; it explicitly lists PingDataSync and
Delegated Admin in that suite. Product versions differ from the lab's image
2609 choices. Compare functionality, licensing and compatibility; do not
downgrade the lab merely to copy the hosted release's version numbers.
[Source: platform 2.2 release notes](https://docs.pingidentity.com/pingoneadvancedservices/2.2/release_notes/p1as_rel_notes_march2026.html).

Ping documents OpenSearch log aggregation, Prometheus-backed Grafana dashboards,
application alerts, SIEM export and proactive infrastructure monitoring. The
local POC currently implements only a subset of that observability function.
[Source: monitoring and logging](https://docs.pingidentity.com/pingoneadvancedservices/2.2/monitoring_and_logging/p1as_monitoring_logging.html).

## Components

| Capability | Local status | Remaining work / evidence |
|---|---|---|
| PingDirectory | Implemented; previously running on EKS | Revalidate restored topology, replication and writes; production assurance gates remain separate |
| Directory Proxy | Implemented; explicit active-colour backends | Complete interruption/drain testing and least-privilege backend credentials |
| PingFederate | Implemented; previously running on EKS | Complete application login/logout and session tests; green's last recorded API credential issue needs rechecking |
| PingAccess | Implemented; previously running on EKS | Native PF proxy is configured on blue; generic protected-application/web-session policy lifecycle remains incomplete |
| PF behind PA at `/identity/` | Blue heartbeat and discovery tested publicly | Native client authentication, logout, old/new issuer handling and paired-colour promotion are not fully accepted |
| Argo CD | Implemented and previously tested | New logging app not live; reviewed sync remains manual by design, which is not itself a gap |
| OpenSearch + Dashboards | Implemented POC; local tests passed | EKS delivery, public `logs.eks.darkedges.com`, real native audit events and product dashboards remain unverified |
| Fluent Bit file/node collectors | Rendered for EKS; Docker delivery tested | Validate real product paths, rotation, permissions and new shared `/opt/out` mounts on restored engines/Proxy |
| PingCentral | Missing | Add application onboarding/templates, connected PF/PA environments, application-team roles and promotion workflows if desired |
| Delegated Admin / PingDelegator | Missing | Add UI, PF authentication/client configuration, Directory REST access and delegated scopes/ACIs; chart variables alone do not deploy it |
| PingDataSync | Missing; use-case dependent | Needed for synchronization with external sources; not needed merely to replicate the existing Directory topology |
| Prometheus / exporters | Missing | Product/JVM metrics, scrape configuration, Kubernetes/node metrics and replicated topology health |
| Grafana | Missing | Product and infrastructure dashboards; distinguish metrics monitoring from OpenSearch log search |
| Alert routing | Missing beyond basic service health | Deliver actionable replication, LDAP connectivity, authentication, disk, certificate and ingestion alerts to an operator |
| Directory REST/SCIM external access | Not established as a complete platform endpoint | Product-native features may exist, but no dedicated ingress, auth/ACI acceptance or Delegator integration is demonstrated |

Evidence: `scripts/gitops.py` lists the seven Ping workload applications;
`gitops/workloads/logging/` adds search/UI/node collection; `build/logging-smoke.json`
records synthetic file/CRI delivery, collector restart, TLS, authentication and
data-view checks. Successful synthetic shipping does not attest native audit
publisher completeness. Stock chart defaults mentioning other product names
are not evidence of those services being enabled.

## Platform capabilities

| Capability | Local evidence | Gap |
|---|---|---|
| Common administrator SSO/MFA | Native PF/PA/Argo accounts; local OpenSearch admin | No unified OIDC/SAML login, MFA enforcement, central role mapping or tested emergency access across tools |
| Admin API OAuth | PF/PA helper scripts construct Basic headers | Add scoped OAuth/token management if reproducing newer hosted administration; test automation and break-glass behavior |
| Hostname/certificate self-service API | Downloaded OpenAPI file only; CLI ingress helpers exist | No local REST API, Swagger UI, authorization, request/audit history or transactional rollback implementation |
| Certificate lifecycle | ACM external certificate; internal CA and leaf generation | Inconsistent PF/PA listener trust remains; no complete renewal, expiry alerting, rotation or revocation workflow |
| Configuration promotion | Git PRs, isolated PF Terraform and operator evidence gates | No complete application configuration promotion/rollback and secret-reference validation across independent environments |
| Environment isolation | Blue/green namespaces in one account/cluster/region | No independently isolated dev/test/stage/prod footprints, access boundaries or environment promotion chain |
| Backups | Retained PVCs, manual clean-shutdown snapshots and private recovery inventories | No scheduled Directory-native backups, remote configuration/key backups, automatic failure alerts or tested periodic restore drills |
| Node/pod recovery | Managed node groups, controllers and per-AZ EBS scheduling | No completed Spot/node/AZ interruption acceptance and no measured application recovery objectives |
| Capacity scaling | Static per-AZ groups; optional On-Demand; proxy HPA scaffolding | No deployed node autoscaler or demand-based product capacity workflow; never auto-scale Directory down without topology work |
| Multi-region recovery | Single region, three AZs | No regional replication/failover, independent regional recovery footprint or global traffic policy; required only if this is a target |
| Audit coverage | EKS control-plane streams enabled in CloudWatch; product-file POC | Native audit stream enablement, parsing, correlation, central control-plane/ALB/CloudTrail ingestion and completeness tests missing |
| Log retention / replay | Explicitly deferred by operator for POC | Intentionally outside current acceptance; later add policies, capacity limits, archival or replay only as required |
| Platform security monitoring | Cloudflare proxy/source restrictions and Kubernetes boundaries | No repository-managed threat detection integration or centralized vulnerability/patch/secret-rotation program |
| Infrastructure state operations | Local Terraform state and private copies | No demonstrated remote locked state, team change coordination or automated drift review |
| Managed service operations | Operator-driven lab | No SRE/on-call coverage, service commitments, customer support/request workflow or formal change/incident process |

The hosted release notes identify a move away from PF/PA Admin API Basic auth.
Treat that as an interoperability/security model difference, not a reason to
break working lab tooling without a tested replacement.
[Source: current release notes](https://docs.pingidentity.com/pingoneadvancedservices/2.2/release_notes/p1as_rel_notes.html).

The downloaded `docs/openapi.json` has ten operations covering hostnames and
certificates, all with Bearer authentication. It is ignored by Git, as requested.
It is a useful contract for a future local API, not an API implementation and
not a specification for every hosted platform operation.

Blue/green colour is a release/routing dimension; it does not replace dev/test/
stage/prod isolation. A separate multi-region topology is likewise not implied
by three-AZ scheduling. Neither single-region nor multi-region operation should
be described as having a measured RPO/RTO until failure tests establish it.

For ongoing Directory backups, the vendor provides native backup/restore tools
and scheduling support; use these for application-aware recovery alongside
tested infrastructure recovery rather than treating retained disks as backups.
[Source: Directory 11.1 backup/restore](https://docs.pingidentity.com/pingdirectory/11.1/pingdirectory_server_administration_guide/pd_ds_backup_restore_data.html).

## Prioritized POC backlog

Items 2–4 now have optional GitOps manifests, runtime secret preparation and
integration instructions in [PLATFORM-SERVICES.md](PLATFORM-SERVICES.md).
They remain subject to the listed live EKS acceptance checks; local preparation
does not mean the hosted-service gaps have been closed.

1. Restore the existing lab, deploy the prepared logging application and test
   delivery from every real Ping pod. Recheck PF green credentials and prove
   a real PA -> PF -> LDAP authentication flow before adding more components.
2. Add product metrics, Prometheus, Grafana and a small set of actionable alerts.
   These give visibility into subsequent integrations and failure testing.
3. Add Delegated Admin with real Directory permissions and a PF login flow.
   This is the clearest missing user-facing component.
4. Add PingCentral if application onboarding/template promotion is a goal;
   confirm licensing and compatibility with the selected PF/PA versions first.
5. Integrate administrator SSO/MFA and role mappings across the expanded tools.
   Keep and test independent emergency access to avoid circular PF dependencies.
6. Build a narrow hostname/certificate API from the supplied OpenAPI contract
   if recreating the self-service experience is important. Keep runtime secrets
   out of Git and distinguish ACM edge certificates from internal listener keys.
7. Add DataSync only with a defined source/target integration and conflict policy.

For production readiness, prioritize backup/restore automation, internal TLS,
capacity management, audit completeness and measured failure/cutover behavior
ahead of additional UI/API features. Multi-region and stronger retention/loss
requirements remain explicit choices, not requirements imposed on the current POC.

## Next live evidence checklist

- Ready licensed pods in both colours, expected image/product versions and
  correct Directory topology after restore.
- Successful real login and logout; intended PA policy enforcement; correct
  metadata/issuer URLs and direct-admin routing.
- Native audit entries for selected successful/failed binds, writes, login and
  administration, reconciled with the events visible in OpenSearch.
- EKS node/file collectors operating without mount, permission or TLS errors.
- Paired-colour cutover/rollback with request results and session behavior,
  followed by a controlled pod/node interruption test.
- A separate restore rehearsal from application backups and preserved secrets;
  retained source disks must not be deleted merely to force the exercise.

No numerical parity score is assigned: component presence, configuration
completeness and managed-service operation are different things.
