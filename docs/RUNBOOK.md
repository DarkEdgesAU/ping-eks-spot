# Blue/green and recovery runbook

## Preparation

Start with blue, then deploy proxy, then green. Both colours use Helm release `pd` in separate namespaces (`identity-blue`, `identity-green`). The stock Kubernetes multi-environment variables reference the existing blue seed and advertise real, fully qualified pod DNS names.

An init container reads the node's Kubernetes AZ label into an emptyDir. The editable pre-setup hook replaces the colour-derived PingDirectory `LOCATION` with the actual AZ before server setup. Its Kubernetes service account can get nodes but cannot mutate them. Inspect each actual server location after first boot; this lifecycle hook is a development implementation requiring licensed runtime validation.

The proxy profile creates explicit external server definitions for all six directory addresses. Only the active colour belongs to the load-balancing algorithm. There is no automatic discovery that can silently add green. Backend administrative binding is development-only and must be restricted before production.

The fewest-operations algorithm sets `use-location:false` and balances across the selected colour's three backends. Directory pod ordinals are not fixed AZ assignments, so external-server locations must not be guessed from ordinal numbers. AZ placement and directory server locations remain independently validated; proxy location preference is not a cross-AZ write assurance policy.

## Promotion

1. Deploy the candidate with a new immutable ECR tag. Leave active colour untouched. Do not reuse an existing ECR image tag.
2. Check every required replication domain using dsreplication and monitor alarms, backlog, current server membership and data correctness. Healthy pods alone are insufficient. Keep the seed and existing replicas available.
3. Exercise representative LDAPS binds/searches/writes against the candidate endpoint and reconcile writes across colours. Check actual deployed image/profile and rollback compatibility. Include direct writers and password-policy changes.
4. Copy `docs/promotion-evidence.example.json` to an ignored `build/` file. Record the evidence, current UTC timestamp and operator, and change flags only after the checks complete. This is an operator attestation, not an automated proof of no data loss.
5. Run `make promote TARGET=green EVIDENCE=build/green-evidence.json` within five minutes of verification. The command checks cluster/target/image identifiers, namespace pod readiness, and three-AZ placement. It persists the target in a ConfigMap before changing each live proxy's backend list.
6. Keep blue running and replicating. Promotion across proxy instances is not instantaneous. Check remaining backend connections and operations on blue; do not assume a changed algorithm guarantees every existing connection has already drained.
7. If a proxy configuration update fails halfway through, both colours may receive operations. Keep both healthy; inspect `pd-routing` and every proxy's `active-directory` algorithm, and repair/re-run promotion. Do not retire either colour while routing is inconsistent.
8. After validation and the rollback window, retire old directory members using supported topology tools. Seed relocation, topology removal, and PVC cleanup are deliberately manual until their acceptance gate passes.

## Rollback

Verify the old colour is still synchronized and compatible with current schema/data. Set `config.json.image_tag` to the old colour's actual deployed tag, create matching fresh evidence, and promote the old colour. This is routing rollback with current data. An old EBS snapshot is not routing rollback and would omit newer writes.

## Retained volume recovery

For normal node loss, the StatefulSet recreates the pod in the EBS volume's AZ and reuses the claim. Keep server identity, secrets and encryption material. Never manually attach the same directory database to concurrent directory processes. If recovery requires a new replica/PVC, follow product initialization and topology replacement procedures rather than cloning one live server's identity.

For a complete AZ outage, serve through surviving AZs. Do not attempt to attach the affected EBS volume in another AZ. If a backup must be restored into another zone, treat it as explicit recovery with data/identity checks and initialization from surviving peers.

## Secrets and cleanup

Runtime Secret files are copied by `prepare-runtime` into a memory-backed emptyDir
before the server starts. The application mounts that copy read-only at
`/run/ping-secrets`; setup can therefore canonicalize filenames without persisting
Kubernetes Secret projection timestamps. Secret changes take effect on a deliberate
pod restart, not through live projection updates. Coordinate certificate/password
rotation across the replication topology and proxy before restarting workloads.

For retained instances created by the earlier direct Secret mount, the same init
step normalizes saved key/trust-store file and PIN paths in `config/config.ldif`
while the server is stopped. It unfolds LDIF lines and preserves the original
configuration as `config.ldif.before-secret-path-repair.<timestamp>` on the PVC.
It does not alter server identity, passwords, encryption settings, or directory data.
This step must never run concurrently with a directory process using the same PVC.

The CA private key stays on the remote developer host in ignored `.secrets/`; it is not mounted into pods. Server key material is in the generated PKCS12 keystore. The development environment shares that server key across replicas. Production needs an approved credential and certificate lifecycle.

Retain local secret material securely; losing encryption settings and their passwords can make retained encrypted data unusable. Do not regenerate secrets over an existing deployment casually. The generator reuses existing files.

Terraform state is local initially. Before team use, configure an existing encrypted S3 backend with locking, restrict access, and migrate state. No credentials or directory passwords are stored in Terraform variables by this project.

To remove the lab, first export and restore-test data, retire directory topology members, and remove Helm releases. Do not delete directory PVCs until data disposal is authorized. ECR repositories intentionally refuse deletion while images remain. Terraform does not own the dynamically provisioned EBS volumes; retained volumes survive ordinary infrastructure deletion and require deliberate cleanup.
