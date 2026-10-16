# Recover the lab after the 14 October teardown

This walkthrough describes the **14 October** backup. For a later teardown,
select that backup and follow its private `RESTORE.md`; do not assume the volume
counts or repository counts below still apply.

The local recovery helper also supports `identity-access-blue`,
`identity-access-green`, `identity-central`, `identity-delegator`, `logging` and
`monitoring`. Capture `<namespace>-resources.json` for every deployed namespace,
including its Secrets and PVCs, together with `persistent-volumes.json`. It
prepares their original runtime Secrets and retained disk bindings, preserving
disk IDs and AZ affinity. It does not generate replacement credentials.
Optional namespace inventories may be absent in historical backups only when
no saved PV claims that namespace; otherwise preparation stops with an error.
Review `prepared/volume-ids.json` and all expected PVC bindings before syncing
any stateful Argo application. Legacy `identity-federate` migration-source disks
remain excluded.

Run these commands in **PowerShell 7**, from this repository. They rebuild AWS
infrastructure and reuse the retained disks; they do not create fresh Directory
or PF databases. Commands below are instructions, not actions already executed.
Stop if a command fails. Infrastructure recreation resumes AWS charges.

The backup is `.secrets/teardown-20261014T101912Z`. It includes ten saved PVs:
eight belong to the current deployments and two are older PF migration sources.
Restore only the eight current bindings. Keep all ten disks and snapshots.
PA was not deployed before teardown and is a separate installation, not recovery.

## 1. Set your working directory and authenticate

```powershell
Set-Location E:\development\projects\ping\pingdirectory-eks-spot
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$backup = '.secrets/teardown-20261014T101912Z'
$prepared = "$backup/prepared"
$env:AWS_PROFILE = 'pingfeddeploy'
$env:AWS_DEFAULT_REGION = 'ap-southeast-2'
Remove-Item Env:AWS_ACCESS_KEY_ID,Env:AWS_SECRET_ACCESS_KEY,Env:AWS_SESSION_TOKEN,Env:AWS_SECURITY_TOKEN -ErrorAction SilentlyContinue

aws sso login --profile pingfeddeploy
aws sts get-caller-identity
```

Confirm account **447061058131**. Keep the original `.secrets/`, local JSON
configuration, `terraform/terraform.tfvars`, and PF Terraform workspace state.
Do not run `make setup`, regenerate passwords/CA/master keys, or restore the old
AWS Terraform state: it describes infrastructure that no longer exists.

```powershell
Test-Path "$backup/persistent-volumes.json"
Test-Path "$backup/terraform-outputs.json"
Test-Path .secrets/ca.key
Test-Path .secrets/ca.crt
py -3 -m pip install -r requirements.txt
```

All four `Test-Path` results must be `True`. You need AWS CLI, Terraform, Helm,
kubectl, Python and an Argo CD CLI compatible with your pinned server version.

## 2. Generate sanitized restore manifests locally

```powershell
py -3 scripts/recovery.py --backup $backup
$volumeIds = Get-Content "$prepared/volume-ids.json" -Raw | ConvertFrom-Json
aws ec2 describe-volumes --region ap-southeast-2 --volume-ids $volumeIds --query 'Volumes[].{ID:VolumeId,State:State,AZ:AvailabilityZone}' --output table
```

Expect **eight volumes**, all `available`, in their original AZs. The helper
does not call AWS or Kubernetes. It removes old object UIDs/resource versions,
owner references and finalizers, preserves disk IDs and AZ affinity, and prepares
prebound PV/PVC pairs with `Retain`. Its generated files include secrets: keep
them under `.secrets/` and do not commit or paste them.

## 3. Reimport retained images and rebuild AWS infrastructure

```powershell
terraform "-chdir=terraform" init
py -3 "$backup/reimport_ecr.py"
terraform "-chdir=terraform" state list
```

Expect the three retained ECR repositories in state. The helper skips addresses
already imported. The new PA repository will be created by the infrastructure
plan; no PA image needs building to restore Directory/PF.

Review your existing `terraform/terraform.tfvars`: preserve cluster name
`pd-spot-dev`, region `ap-southeast-2`, all three AZs and `enable_federate=true`
(this provisions the ALB-controller role). Then create a **new** plan:

```powershell
New-Item -ItemType Directory -Force build | Out-Null
terraform "-chdir=terraform" plan "-out=../build/recovery-infrastructure.tfplan"
terraform "-chdir=terraform" show "../build/recovery-infrastructure.tfplan"
```

Review the output. It should create the destroyed infrastructure and PA ECR
repository, not replace/delete the three imported image repositories or data
volumes. Apply only that reviewed plan:

```powershell
terraform "-chdir=terraform" apply "../build/recovery-infrastructure.tfplan"
make kubeconfig
kubectl config current-context
kubectl get nodes -L topology.kubernetes.io/zone
kubectl -n kube-system rollout status deployment/ebs-csi-controller --timeout=300s
```

The context must identify `pd-spot-dev` in account 447061058131. Verify worker
capacity in `ap-southeast-2a`, `ap-southeast-2b`, and `ap-southeast-2c`.

## 4. Restore namespaces, RBAC, storage and original runtime secrets

```powershell
kubectl apply -f "$prepared/00-namespaces.yaml"
py -3 -c "import sys; sys.path.insert(0, 'scripts'); import project; project.check_context(project.config()); project.bootstrap(project.config())"
kubectl apply -f "$prepared/20-runtime-secrets.yaml"

# Imported EBS disks need this tag for the CSI IAM policy.
aws ec2 create-tags --region ap-southeast-2 --resources $volumeIds --tags 'Key=ebs.csi.aws.com/cluster,Value=true'
kubectl apply -f "$prepared/30-retained-claims.yaml"
```

The bootstrap Python command creates only namespaces, Directory service
accounts/node-zone RBAC and the `pingdirectory-ebs` Retain StorageClass. It does
not start pods or generate secrets. **Do not run `make secrets` or
`make pf-secrets` during this recovery:** the saved Kubernetes Secrets are the
ones that match the retained data.

Require all eight claims to bind before registering/syncing workloads:

```powershell
$dataNamespaces = @('identity-blue','identity-green','identity-federate-blue','identity-federate-green')
foreach ($ns in $dataNamespaces) {
    kubectl -n $ns wait '--for=jsonpath={.status.phase}=Bound' pvc --all --timeout=120s
    kubectl -n $ns get pvc
}
kubectl get pv
```

Expected: three Directory claims per colour and one PF admin claim per colour.
Every restored PV must show `Retain`. If any claim is `Pending`, **stop here**;
do not let a StatefulSet create an empty replacement claim.

## 5. Bootstrap Argo CD and the ALB controller

```powershell
make pf-controller
make argocd-secrets
make argocd-bootstrap
kubectl apply -f "$prepared/40-runtime-configmaps.yaml"
```

`argocd-secrets` reuses existing local Argo TLS files and the shared CA; it does
not regenerate Directory/PF secrets. Argo's own admin account is fresh, while
saved repository credentials are restored. The runtime ConfigMaps restore
Directory Proxy's active colour (blue at teardown) and saved Argo certificate
trust. Bootstrap Helm-managed Argo secrets/release records are not replayed.

In a **second PowerShell window**, keep this command running:

```powershell
kubectl -n argocd port-forward service/argocd-server 8443:443
```

Back in the first window, copy the new Argo admin password to your clipboard
and log in interactively (paste when prompted):

```powershell
$passwordBase64 = kubectl -n argocd get secret argocd-initial-admin-secret -o 'jsonpath={.data.password}'
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($passwordBase64)) | Set-Clipboard
argocd login localhost:8443 --username admin --grpc-web --server-crt .secrets/ca.crt
Set-Clipboard -Value ''
```

## 6. Publish manifests and register only the five recovery applications

```powershell
make gitops-export
make validate
git diff -- gitops/
```

The exports include seven applications, but **do not register/sync PA yet**.
Review and push the manifest changes to the configured `feature/pingfederate`
branch so Argo can fetch them:

```powershell
git add gitops/
git diff --cached --stat
git commit -m "Update GitOps manifests for retained-volume recovery"
git push origin feature/pingfederate
```

If there are no staged changes, skip the commit and ensure the remote branch
already contains the expected manifests. Keep unrelated staged changes out of
this commit. Register only Directory, Proxy and PF:

```powershell
kubectl apply -f gitops/applications/project.yaml
foreach ($app in @('pd-blue','pd-green','pd-proxy','pf-blue','pf-green')) {
    kubectl apply -f "gitops/applications/$app.yaml"
}
kubectl -n argocd get applications
```

Applications are manual sync and have no cascading deletion finalizer. Do not
enable pruning or auto-sync during recovery.

## 7. Sync in dependency order, without pruning

```powershell
argocd app sync pd-blue --grpc-web
argocd app wait pd-blue --sync --health --timeout 900 --grpc-web
argocd app sync pd-green --grpc-web
argocd app wait pd-green --sync --health --timeout 900 --grpc-web
argocd app sync pd-proxy --grpc-web
argocd app wait pd-proxy --sync --health --timeout 900 --grpc-web

make status
```

Stop if Directory replication/status checks fail. Confirm the original data,
topology and active proxy colour before proceeding. Then start PF:

```powershell
argocd app sync pf-blue --grpc-web
argocd app wait pf-blue --sync --health --timeout 900 --grpc-web
argocd app sync pf-green --grpc-web
argocd app wait pf-green --sync --health --timeout 900 --grpc-web
kubectl -n identity-federate-blue get pods,pvc
kubectl -n identity-federate-green get pods,pvc
```

The restored blue PF volume contains its repaired baseline. Green restores its
own previous configuration; this procedure does not copy blue configuration to
green or rerun an import. Do not run PF configuration Terraform merely as a
restore step. Verify blue's existing account, license and baseline first.

## 8. Recreate public ingress and update Cloudflare DNS

Ensure `federate.json` still sets `initial_colour` to `blue`, matching teardown,
and the local JSON files still contain the issued ACM ARN and Cloudflare ranges.

```powershell
make argocd-ingress
make pf-ingress
kubectl get ingress -A -o wide
```

Replace Cloudflare's CNAME targets with the **new** ALB DNS addresses:

| Cloudflare record | New target |
|---|---|
| `argocd.eks` | Argo ingress ADDRESS |
| `pingfederate-admin.eks`, `pingfederate-blue-admin.eks`, `pingfederate-green-admin.eks` | PF admin ingress ADDRESS |
| `id.eks` | PF engine ingress ADDRESS |

Keep these records proxied, use Full (strict), retain the ACM DNS validation
CNAME and bypass caching. Do not point these records at the deleted ALB names.
Verify both admin consoles, the preserved blue baseline, Directory LDAPS and a
real PF authentication flow before declaring recovery complete. Ready pods
alone do not verify licenses, replication or authentication/session behavior.

Do not delete the older PF volumes, any snapshot, or the teardown inventory as
part of this procedure. Reimported image repositories return to Terraform
ownership and will need the same preservation decision on the next teardown.
