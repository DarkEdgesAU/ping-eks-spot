# Working on this project with Codex

Goal: Terraform-managed EKS, Spot capacity, Ping DevOps profiles and safe live blue/green LDAPS routing.

Read README.md and docs/ROADMAP.md before changing deployment behavior. Run `make validate` after code/profile/value changes, and `make terraform-check` after Terraform changes.

- Never commit passwords, licenses, certificates with private keys, AWS credentials, Terraform state, or rendered secrets.
- Keep infrastructure Terraform and workload deployment separate; avoid Kubernetes-provider bootstrap dependencies in the AWS stack.
- Keep one EBS PVC per directory replica, Retain reclaim policy, explicit three-AZ scheduling, and zone-specific worker capacity.
- Do not auto-scale down directory replicas or delete PVCs. Retirement requires topology management and verified replication.
- Green must join blue's topology. Do not introduce independent genesis servers.
- Proxy backend lists are explicit. Green cannot enter routing until promotion. Preserve the active colour on proxy restarts.
- Do not claim RPO=0 from a PDB, local EBS durability, successful LDAP result code, or ordinary/assured replication alone.
- Cross-AZ assurance enforcement and interruption testing remain explicit production acceptance gates.
- Run plans and tests locally. Do not apply infrastructure, send messages, publish images, or deploy to a real account unless the user requests those actions.
- Do not delegate unless the user asks for parallel agents.
