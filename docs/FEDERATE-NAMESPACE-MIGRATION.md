# PF namespace migration with configuration preservation

Target: `identity-federate-blue` and `identity-federate-green`. The existing
`identity-federate` namespace and PVCs remain intact. Never sync new admins onto
empty claims, prune the old resources, or attach the same disk to two admins.
Use a maintenance window and freeze admin configuration. Disk snapshots preserve
configuration/keys/data, not engine memory or in-flight sessions.

## Preparation

Disable auto-sync for both PF Applications and stop concurrent sync/Terraform
operations. Leave Directory unchanged. Generate read-only inventory:

```powershell
py -3 scripts/federate_migration.py --target blue
py -3 scripts/federate_migration.py --target green
```

This verifies the Kubernetes context and records the source disk/AZ in ignored
`build/pf-<colour>-migration.json`. Authenticate AWS CLI in the same account if
inventory fails. Preserve `.secrets/` and existing Terraform workspaces. Do not
regenerate cluster passwords, signing keys or master keys. Record old routing.

The migration's master-key preservation hook is supplied by a reviewed GitOps
ConfigMap mounted over the bootstrap hook; no image rebuild is needed. Keep
existing image tags and the base product version fixed during migration.

## Restore each admin's full volume

Run the following for blue, then repeat for green by changing `$colour`.

```powershell
$colour = 'blue'
$plan = Get-Content "build/pf-$colour-migration.json" | ConvertFrom-Json
kubectl -n $plan.old_namespace scale statefulset $plan.statefulset --replicas=0
if ($LASTEXITCODE -ne 0) { throw 'Admin stop failed' }
kubectl -n $plan.old_namespace wait --for=delete "pod/$($plan.statefulset)-0" --timeout=180s
if ($LASTEXITCODE -ne 0) { throw 'Admin pod still exists' }
aws ec2 wait volume-available --region $plan.region --volume-ids $plan.source_volume_id
if ($LASTEXITCODE -ne 0) { throw 'Source disk is still attached' }
$snapshot = aws ec2 create-snapshot --region $plan.region --volume-id $plan.source_volume_id --description "PF $colour namespace migration" --query SnapshotId --output text
if ($LASTEXITCODE -ne 0) { throw 'Snapshot creation failed' }
aws ec2 wait snapshot-completed --region $plan.region --snapshot-ids $snapshot
if ($LASTEXITCODE -ne 0) { throw 'Snapshot did not complete' }
$restored = aws ec2 create-volume --region $plan.region --snapshot-id $snapshot --availability-zone $plan.availability_zone --volume-type gp3 --query VolumeId --output text
if ($LASTEXITCODE -ne 0) { throw 'Volume restore failed' }
aws ec2 wait volume-available --region $plan.region --volume-ids $restored
if ($LASTEXITCODE -ne 0) { throw 'Restored volume unavailable' }
py -3 scripts/federate_migration.py --target $colour --restored-volume-id $restored
```

Snapshot only a cleanly stopped, detached source. Keep its original claim bound.
The final command verifies the restored disk came from the exact source's
completed snapshot, is unattached in the same AZ, and preserves encryption.
It writes a Retain PV/pre-bound PVC manifest without applying anything.
Record snapshot/new disk IDs: they incur storage charges until disposed of.

Create the new namespaces/runtime Secrets with updated code and bind each
restored volume **before** Argo creates its new StatefulSet:

```powershell
make pf-secrets
# Review this manifest and its source/target disk IDs first.
kubectl apply -f "build/pf-$colour-restored-claims.yaml"
kubectl -n $plan.new_namespace get pvc
```

Repeat for the other colour. Old admins can be restarted at one replica after
snapshots complete if needed for rollback; keep configuration frozen so newer
changes are not lost from the restored copy.

## Deploy and verify

Export/commit/push the new namespaces and image tags; run
`make argocd-applications` to update project destinations. Manually sync each PF
Application with pruning disabled. The new admin must use the restored claim,
not a fresh dynamically provisioned disk. Old namespace resources remain
orphaned and retained. Do not prune or delete them.

The new bootstrap hook preserves existing master keys; restart does not re-import the baseline.
Compare admin configuration with the original before any Terraform apply. The
existing per-colour Terraform workspaces remain; its wrapper now forwards to
the new namespace. Do not accept an unexpected reset in a configuration plan.

Reissue restored SSL certificates for the new internal DNS names through PF's
supported CSR/certificate-response workflow, using the internal CA. Keep the
existing private key and certificate ID. Fresh bootstrap TLS Secrets do not
automatically replace certificates saved on a restored admin. Verify certificate
SANs, master-key compatibility, config replication, LDAP, login and all engines.
Do not expose the new route before verification.

## Route and retain rollback

The two namespaces share ALB IngressGroups `pf-admin` and `pf-engine`. Rules
target local Services. Active rules forward at priority 0; standby rules return
503 at priority 100. Gated promotion updates these rules sequentially, not
atomically. There are no cross-namespace Service selectors.

After verifying the restored initial colour, `make pf-ingress` creates new
grouped ALBs. It does not adopt the legacy ungrouped ALBs. Keep old ALBs/DNS for
rollback until the new ALBs are healthy, then change Cloudflare targets through
its reviewed Terraform workflow. Keep Access/certificate protection in place.

Retire old workloads/ingresses only after checks and the rollback window.
Existing admin PVCs, snapshots and the old namespace remain until separate data
disposal authorization. No command above deletes them. Directory is unaffected.
