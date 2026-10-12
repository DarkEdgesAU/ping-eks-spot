# Current project architecture

Infrastructure Terraform owns AWS resources; scripts/project.py owns Kubernetes/Helm deployment. Profiles are baked into custom images.

| Component | Normal operation | During deployment |
| --- | --- | --- |
| EKS control plane | Managed AWS service | Same control plane |
| Workers | One Spot group per AZ, two desired nodes each | Increase capacity explicitly if needed |
| Blue | Three replicas across three AZs, independent EBS PVCs | Remains online and replicating |
| Green | Absent initially | Three replicas across three AZs, new PVCs |
| DirectoryProxy | Three replicas across three AZs | Routing switches to the candidate |
| Endpoint | Proxy service, development port-forward | Same endpoint |

Each Helm release is named pd in its own namespace: identity-blue, identity-green and identity-proxy. Green references the existing blue seed. The AZ init container and pre-setup hook assign physical server locations. Verify the resulting topology and locations after licensed deployment; Kubernetes placement alone does not establish directory locations.

Each directory replica owns its own /opt/out volume. Processes never share backend files. StatefulSet replacement reuses the original PVC in its EBS AZ; surviving replicas serve other AZs. Retain prevents automatic backing-storage deletion on claim deletion, but does not implement backup or cross-AZ write acknowledgment.

The proxy uses explicit backend definitions and the persisted pd-routing active-colour ConfigMap. Green stays out of the active backend list until promotion; restarted proxies read the selected colour. Live routing changes and existing operations can overlap across colours. Keep both online for drain and rollback.

The assured-replication profile example is assistance, not enforcement of safe success responses. Zero-loss AZ protection needs the ROADMAP.md gates. The starter does not automatically reject/queue writes when assurance is unmet or retire directory topology members.

Optional On-Demand groups can host directory pods while proxies stay on Spot. Defaults implement the user's Spot development requirement.
