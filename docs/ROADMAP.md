# Development roadmap

## Included

AWS VPC, private subnets and independent NAT egress per AZ; EKS API access entries; one managed Spot node group per AZ; optional On-Demand groups; EBS CSI with Pod Identity; ECR repositories; vendored Ping chart; editable baked server profiles; separate blue/green volumes; proxy routing persistence; operator-gated promotion; remote dev container and CI validation.

The initial deployment is a development environment. It does not establish a production zero-loss guarantee.

## Next implementation gates

PingAccess blue/green scaffolding and acceptance steps are in
[PINGACCESS.md](PINGACCESS.md). Licensed bootstrap, shared-CA listener assignment,
verified engine registration, PingFederate integration and protected application
login/session cutover remain required live acceptance work.

Argo CD bootstrap and protected GitOps exports are documented in
[ARGOCD.md](ARGOCD.md). Live Helm adoption, ordered sync, Cloudflare Access and
ALB ingress testing remain acceptance gates.

1. Run a licensed deployment and validate profile commands, stock replication bootstrap, three AZ locations, TLS trust and permissions. Container lifecycle commands cannot be exercised offline with Helm rendering alone.
2. Replace the shared development key/certificate and administrative proxy bind with the organization's per-instance certificate lifecycle and least-privilege proxy bind identity.
3. Automate LDAP write probes and acknowledged-write reconciliation; capture reports for every replicated base DN. Replace manual promotion attestations with machine-verifiable candidate/image evidence.
4. Implement and prove a mandatory cross-AZ acknowledgment policy for every relevant write origin. Native assured replication may return LDAP success after assurance failure or with unavailable peers excluded. Define reject/queue/uncertain-outcome behavior. Never label a simple proxy routing or assurance-policy setting RPO=0.
5. Add directory-native backup scheduling, encrypted remote archival, retention, encryption-settings preservation and restore tests. EBS Retain and replication are not backup.
6. Exercise single node/Spot interruption, drained node replacement, abrupt AZ loss, delayed replication, an isolated writer, promotion under load, and rollback. Verify all acknowledged operations and record recovery times.
7. Add topology-aware retirement and replacement of retained-volume slots, including seed relocation. Do not automate PVC deletion or StatefulSet scale-down before this gate passes.
8. Add node autoscaling (Cluster Autoscaler or Karpenter) with topology-aware capacity and controlled disruption. Current managed groups replace failed nodes but do not automatically scale to unschedulable demand. Keep directory replica count explicit. Proxy HPA needs metrics and connection behavior validation.
9. Add approved internal NLB provisioning/controller and enterprise DNS. The starter uses port-forwarding for development and creates no public directory endpoint.
10. Validate each major-version transition separately; enable new-version-only features after the old colour is retired. Replicated schema/data changes affect both colours.

## Spot behavior

PingFederate blue/green rendering, profile bootstrap, ALB ingress, and isolated
configuration Terraform are described in [PINGFEDERATE.md](PINGFEDERATE.md).
Licensed baseline import, runtime cluster encryption, session/token continuity,
Cloudflare admin-origin protection, and ALB cutover testing remain acceptance
gates; offline validation does not establish them.

Each node group is confined to one AZ so a replacement node can attach an existing EBS volume there. Multiple compatible instance types diversify allocation. AWS managed groups use Capacity Rebalancing and handle interruption notices, but notice and orderly completion are not guaranteed. A PDB does not fence an isolated writer or prevent termination. Empty Spot capacity in an AZ can delay attachment/recovery; the other two AZs must be sized to carry traffic.

You may set Terraform `enable_on_demand=true` and config `directory_capacity_type=ON_DEMAND` to keep directory replicas on non-Spot groups while proxies remain on Spot. Both are optional; the packaged default is Spot throughout the workload nodes.

The EKS control plane, EBS, NAT gateways, and other managed services are not Spot resources. No automatic production deployment is configured.
