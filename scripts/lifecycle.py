#!/usr/bin/env python3
"""Explicit, serialized lab backup/setup/shutdown. Sensitive output is suppressed."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

import recovery
from vault_runtime import Vault, import_secrets, read_secrets, import_bootstrap_files

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = ('config.json', 'federate.json', 'access.json', 'observability.json',
                'platform-services.json', 'gitops/config.json', 'terraform/terraform.tfvars',
                'terraform-federate/terraform.tfstate',
                'terraform-federate/terraform.tfstate.d/blue/terraform.tfstate',
                'terraform-federate/terraform.tfstate.d/green/terraform.tfstate')
NAMESPACES = (*recovery.WORKLOAD_NAMESPACES, 'argocd')


def command(args, data=None):
    result = subprocess.run([str(x) for x in args], input=data, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    if result.returncode:
        raise ValueError(f'Command failed: {Path(str(args[0])).name} (exit {result.returncode}); output suppressed')
    return result.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def argocd_tls_from_files(files):
    """Restore existing listener identities from version-pinned bootstrap files."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    def read(name):
        if name not in files:
            raise ValueError('Saved Argo TLS material is missing')
        return base64.b64decode(files[name], validate=True)
    ca_pem = read('.secrets/ca.crt')
    ca = x509.load_pem_x509_certificate(ca_pem)
    result = []
    for name in ('argocd-server-tls', 'argocd-repo-server-tls'):
        cert_pem = read('.secrets/argocd/'+name+'.crt')
        key_pem = read('.secrets/argocd/'+name+'.key')
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=None)
        def public_bytes(public):
            return public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if public_bytes(cert.public_key()) != public_bytes(key.public_key()):
            raise ValueError('Saved Argo TLS key does not match its certificate')
        cert.verify_directly_issued_by(ca)
        now = dt.datetime.now(dt.timezone.utc)
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError('Saved Argo TLS certificate is not currently valid')
        chain = cert_pem if ca_pem in cert_pem else cert_pem+ca_pem
        result.append({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': 'argocd'},
            'type': 'kubernetes.io/tls', 'data': {k: base64.b64encode(v).decode() for k, v in {
                'tls.key': key_pem, 'tls.crt': chain, 'ca.crt': ca_pem}.items()}})
    return result


def validate_config(c):
    if c['account_id'] != '447061058131' or c['region'] != 'ap-southeast-2' or c['cluster_name'] != 'pd-spot-dev':
        raise ValueError('Unexpected lab account/region/cluster')
    for name in ('state_bucket', 'backup_bucket', 'kms_key_arn', 'state_key'):
        if not c.get(name) or 'REPLACE' in c[name]:
            raise ValueError('Configure lifecycle resource: '+name)
    if c.get('consistency') not in ('crash-consistent', 'application-consistent'):
        raise ValueError('Select an explicit backup consistency level')
    if c['consistency'] == 'application-consistent' and not c.get('pre_backup_commands'):
        raise ValueError('Application-consistent backup requires reviewed quiescence/native-backup commands')
    if not c.get('verification_commands'):
        raise ValueError('Configure application verification commands before lifecycle execution')
    return c


def validate_manifest(m, c):
    if m.get('schema') != 1 or m.get('status') != 'complete':
        raise ValueError('Backup is not complete')
    for key in ('account_id', 'region', 'cluster_name'):
        if m.get(key) != c[key]:
            raise ValueError('Backup identity mismatch: '+key)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', m['backup_id']):
        raise ValueError('Invalid backup ID')
    if len({x['volume_id'] for x in m['volumes']}) != len(m['volumes']):
        raise ValueError('Duplicate retained volume')


def validate_inventory(docs):
    routing = [x for x in docs.get('identity-proxy-resources.json', {}).get('items', [])
               if x['kind'] == 'ConfigMap' and x['metadata']['name'] == 'pd-routing']
    if len(routing) != 1 or routing[0].get('data', {}).get('active_colour') not in ('blue', 'green'):
        raise ValueError('Missing or ambiguous active Directory routing')
    for namespace in ('identity-blue', 'identity-green'):
        claims = [x for x in docs[namespace+'-resources.json']['items'] if x['kind'] == 'PersistentVolumeClaim']
        if len(claims) != 3:
            raise ValueError('Expected three retained Directory claims per colour')
    for namespace in NAMESPACES:
        resources = docs.get(namespace+'-resources.json', {}).get('items', [])
        names = {x['metadata']['name'] for x in resources if x['kind'] == 'Secret'}
        if namespace in ('identity-blue', 'identity-green', 'identity-proxy') and 'pd-runtime' not in names:
            raise ValueError('Missing Directory runtime Secret')
        for resource in resources:
            if resource['kind'] not in ('Deployment', 'StatefulSet') or resource['spec'].get('replicas', 1) == 0:
                continue
            spec = resource['spec']['template']['spec']
            required = {v['secret']['secretName'] for v in spec.get('volumes', [])
                        if 'secret' in v and not v['secret'].get('optional', False)}
            required.update(source['secret']['name'] for v in spec.get('volumes', [])
                for source in v.get('projected', {}).get('sources', [])
                if 'secret' in source and not source['secret'].get('optional', False))
            for container in [*spec.get('containers', []), *spec.get('initContainers', [])]:
                required.update(e['valueFrom']['secretKeyRef']['name'] for e in container.get('env', [])
                    if 'secretKeyRef' in e.get('valueFrom', {}) and not e['valueFrom']['secretKeyRef'].get('optional', False))
                required.update(e['secretRef']['name'] for e in container.get('envFrom', [])
                    if 'secretRef' in e and not e['secretRef'].get('optional', False))
            required.update(s['name'] for s in spec.get('imagePullSecrets', []))
            if required-names:
                raise ValueError('Workload references a missing runtime Secret')


def envelope_encrypt(kms, key, value, backup_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    context = {'project': 'pd-spot-dev', 'backup_id': backup_id}
    material = kms.generate_data_key(KeyId=key, KeySpec='AES_256', EncryptionContext=context)
    nonce = os.urandom(12)
    cipher = AESGCM(material['Plaintext']).encrypt(nonce, encoded(value), encoded(context))
    return encoded({'key': base64.b64encode(material['CiphertextBlob']).decode(),
                    'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(cipher).decode()})


def envelope_decrypt(kms, key, raw, backup_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    value = json.loads(raw)
    context = {'project': 'pd-spot-dev', 'backup_id': backup_id}
    material = kms.decrypt(KeyId=key, CiphertextBlob=base64.b64decode(value['key']), EncryptionContext=context)
    return json.loads(AESGCM(material['Plaintext']).decrypt(base64.b64decode(value['nonce']),
                      base64.b64decode(value['ciphertext']), encoded(context)))


class Lifecycle:
    def __init__(self, c):
        import boto3
        self.c = validate_config(c)
        session = boto3.Session(region_name=c['region'])
        self.s3, self.kms, self.ec2, self.eks = [session.client(x) for x in ('s3', 'kms', 'ec2', 'eks')]
        identity = session.client('sts').get_caller_identity()
        if identity['Account'] != c['account_id']:
            raise ValueError('Wrong AWS account')
        self.context = f"arn:aws:eks:{c['region']}:{c['account_id']}:cluster/{c['cluster_name']}"

    def kube(self, *args, data=None):
        return command(['kubectl', '--context', self.context, *args], data)

    def object(self, key):
        return self.s3.get_object(Bucket=self.c['backup_bucket'], Key=key)['Body'].read()

    def put(self, key, raw, immutable=False):
        return self.s3.put_object(Bucket=self.c['backup_bucket'], Key=key, Body=raw,
            ServerSideEncryption='aws:kms', SSEKMSKeyId=self.c['kms_key_arn'],
            **({'IfNoneMatch': '*'} if immutable else {}))

    @contextlib.contextmanager
    def lock(self):
        key = 'lifecycle/operation.lock'
        lock = self.put(key, encoded({'owner': str(uuid.uuid4()), 'started_at': dt.datetime.now(dt.timezone.utc).isoformat()}), True)
        try:
            yield
        finally:
            self.s3.delete_object(Bucket=self.c['backup_bucket'], Key=key, IfMatch=lock['ETag'])

    def init_backend(self):
        command(['terraform', '-chdir=terraform', 'init', '-input=false', '-reconfigure',
                 '-backend-config=bucket='+self.c['state_bucket'], '-backend-config=key='+self.c['state_key'],
                 '-backend-config=region='+self.c['region'], '-backend-config=encrypt=true',
                 '-backend-config=use_lockfile=true', '-backend-config=kms_key_id='+self.c['kms_key_arn']])

    def kubeconfig(self):
        command(['aws', 'eks', 'update-kubeconfig', '--region', self.c['region'], '--name', self.c['cluster_name']])
        actual = self.kube('get', 'namespace', 'kube-system', '-o', 'json')
        if not json.loads(actual).get('metadata', {}).get('uid'):
            raise ValueError('EKS API unavailable')

    def inventory(self):
        docs = {'persistent-volumes.json': json.loads(self.kube('get', 'pv', '-o', 'json'))}
        present = {x['metadata']['name'] for x in json.loads(self.kube('get', 'namespaces', '-o', 'json'))['items']}
        for namespace in NAMESPACES:
            if namespace in present:
                docs[namespace+'-resources.json'] = json.loads(self.kube('-n', namespace, 'get',
                    'secrets,configmaps,pvc,ingress,deployments,statefulsets,services', '-o', 'json'))
        return docs

    def prepare(self, docs):
        return recovery.prepare_inventory(docs)

    def local_files(self):
        files = {}
        for name in CONFIG_FILES:
            path = ROOT/name
            if path.exists():
                if not path.resolve().is_relative_to(ROOT.resolve()):
                    raise ValueError('Bootstrap file resolves outside the repository')
                files[name] = base64.b64encode(path.read_bytes()).decode()
        # Backup original bootstrap material as well as runtime Secrets. Exclude
        # old backups/review captures; do not recursively archive unrelated files.
        private = ROOT/'.secrets'
        if private.exists():
            for path in private.rglob('*'):
                rel = path.relative_to(private)
                if path.is_file() and not path.is_symlink() and (
                    len(rel.parts) == 1 or rel.parts[0] in ('argocd', 'logging', 'platform', 'oauth-clients',
                                                         'pf-blue', 'pf-green', 'pa-blue', 'pa-green')):
                    if not path.resolve().is_relative_to(ROOT.resolve()):
                        raise ValueError('Private file resolves outside the repository')
                    files['.secrets/'+rel.as_posix()] = base64.b64encode(path.read_bytes()).decode()
        return files

    def vault(self, writable=False):
        vault = Vault(address=self.c['vault_address'])
        vault.login('ping-eks-backup' if writable else 'ping-eks-restore', 'ping-eks-lifecycle')
        return vault

    def hydrate_bootstrap(self):
        vault = self.vault()
        try:
            saved = vault.call('ping-eks/data/lab/bootstrap/files')['data']['data']['files']
        finally:
            vault.close()
        # Fresh ephemeral runners must recover configuration and keys, never replace
        # the bootstrap record with an empty checkout. Explicit local files win.
        files = {**saved, **self.local_files()}
        for required in ('config.json', 'terraform/terraform.tfvars', '.secrets/ca.key', '.secrets/ca.crt'):
            if required not in files:
                raise ValueError('Required bootstrap file is missing')
        self.restore_files(files)

    def backup(self, backup_id, inventory_path=None):
        print('Checking/capturing backup:', backup_id, flush=True)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', backup_id):
            raise ValueError('Invalid backup ID')
        prefix = 'backups/'+backup_id+'/'
        # A completed ID is immutable; retries verify and reuse it.
        try:
            m = json.loads(self.object(prefix+'manifest.json'))
            self.check_backup(m)
            return m
        except self.s3.exceptions.NoSuchKey:
            pass
        try:
            pending = json.loads(self.object(prefix+'pending.json'))
        except self.s3.exceptions.NoSuchKey:
            pending = None
        if pending:
            raw = self.object(prefix+'bundle.json.enc')
            if digest(raw) != pending['bundle_sha256']:
                raise ValueError('Pending backup checksum mismatch')
            bundle = envelope_decrypt(self.kms, self.c['kms_key_arn'], raw, backup_id)
            docs, records = bundle['inventories'], bundle['vault_versions']
            bootstrap_version = bundle['bootstrap_version']
            cluster_uid = bundle['cluster_uid']
        else:
            if inventory_path:
                docs = {ns+'-resources.json': json.loads((inventory_path/(ns+'-resources.json')).read_text())
                        for ns in NAMESPACES if (inventory_path/(ns+'-resources.json')).is_file()}
                docs['persistent-volumes.json'] = json.loads((inventory_path/'persistent-volumes.json').read_text())
            else:
                self.hydrate_bootstrap()
                self.kubeconfig()
                for args in self.c.get('pre_backup_commands', []):
                    command(args)
                docs = self.inventory()
            cluster_uid = None if inventory_path else json.loads(self.kube('get', 'namespace', 'kube-system', '-o', 'json'))['metadata']['uid']
            validate_inventory(docs)
            prepared, _ = self.prepare(docs)
            secrets = prepared['20-runtime-secrets.yaml']
            files = self.local_files()
            vault = self.vault(True)
            try:
                records = import_secrets(vault, 'ping-eks', 'lab', secrets)
                if read_secrets(vault, records) != secrets:
                    raise ValueError('Vault verification failed')
                bootstrap_version = import_bootstrap_files(vault, files)
            finally:
                vault.close()
            bundle = {'inventories': docs, 'files': files, 'vault_versions': records,
                      'bootstrap_version': bootstrap_version, 'cluster_uid': cluster_uid}
            raw = envelope_encrypt(self.kms, self.c['kms_key_arn'], bundle, backup_id)
            self.put(prefix+'bundle.json.enc', raw)
            self.put(prefix+'pending.json', encoded({'bundle_sha256': digest(raw)}), True)
        validate_inventory(docs)
        # Preserve legacy retained EBS volumes too, without deploying their old namespace.
        ids = sorted({pv['spec']['csi']['volumeHandle'] for pv in docs['persistent-volumes.json']['items']
                      if pv.get('spec', {}).get('csi', {}).get('driver') == 'ebs.csi.aws.com'})
        volumes = []
        print('Snapshotting retained volumes:', len(ids), flush=True)
        for volume_id in ids:
            volume = self.ec2.describe_volumes(VolumeIds=[volume_id])['Volumes'][0]
            if not volume['Encrypted']:
                raise ValueError('Retained volume is not encrypted')
            snapshots = self.ec2.describe_snapshots(OwnerIds=['self'], Filters=[
                {'Name': 'tag:LifecycleBackup', 'Values': [backup_id]},
                {'Name': 'volume-id', 'Values': [volume_id]}])['Snapshots']
            if len(snapshots) > 1:
                raise ValueError('Ambiguous snapshot retry')
            snapshot = snapshots[0] if snapshots else self.ec2.create_snapshot(VolumeId=volume_id,
                Description='Ping EKS lifecycle '+backup_id, TagSpecifications=[{'ResourceType': 'snapshot',
                'Tags': [{'Key': 'Project', 'Value': self.c['cluster_name']}, {'Key': 'LifecycleBackup', 'Value': backup_id}]}])
            self.ec2.create_tags(Resources=[volume_id], Tags=[{'Key': 'LifecycleBackup', 'Value': backup_id}])
            volumes.append({'volume_id': volume_id, 'snapshot_id': snapshot['SnapshotId'],
                'az': volume['AvailabilityZone'], 'size': volume['Size'], 'kms_key_id': volume.get('KmsKeyId')})
        m = {'schema': 1, 'status': 'complete', 'backup_id': backup_id,
             **{k: self.c[k] for k in ('account_id', 'region', 'cluster_name', 'consistency')},
             'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'volumes': volumes,
             'bundle_sha256': digest(raw), 'vault_versions': records,
             'bundle_kms_key_arn': self.c['kms_key_arn'],
             'bootstrap_version': bootstrap_version, 'cluster_uid': cluster_uid}
        self.wait_snapshots(m)
        self.check_backup(m)
        self.put(prefix+'manifest.json', encoded(m), True)
        print('Backup complete:', backup_id, '; retained volumes:', len(volumes))
        return m

    def wait_snapshots(self, m):
        if m['volumes']:
            print('Waiting for snapshots:', len(m['volumes']), flush=True)
            self.ec2.get_waiter('snapshot_completed').wait(SnapshotIds=[v['snapshot_id'] for v in m['volumes']],
                                                        WaiterConfig={'Delay': 15, 'MaxAttempts': 240})

    def check_backup(self, m):
        validate_manifest(m, self.c)
        raw = self.object('backups/'+m['backup_id']+'/bundle.json.enc')
        if digest(raw) != m['bundle_sha256']:
            raise ValueError('Backup checksum mismatch')
        bundle = envelope_decrypt(self.kms, self.c['kms_key_arn'], raw, m['backup_id'])
        validate_inventory(bundle['inventories'])
        self.prepare(bundle['inventories'])
        if m['vault_versions'] != bundle['vault_versions']:
            raise ValueError('Backup secret version index mismatch')
        if m['bootstrap_version'] != bundle['bootstrap_version'] or m['cluster_uid'] != bundle['cluster_uid']:
            raise ValueError('Backup bootstrap identity mismatch')
        handles = {pv['spec']['csi']['volumeHandle'] for pv in bundle['inventories']['persistent-volumes.json']['items']
                   if pv.get('spec', {}).get('csi', {}).get('driver') == 'ebs.csi.aws.com'}
        if handles != {v['volume_id'] for v in m['volumes']}:
            raise ValueError('Backup volume inventory mismatch')
        snapshots = self.ec2.describe_snapshots(SnapshotIds=[v['snapshot_id'] for v in m['volumes']])['Snapshots'] if m['volumes'] else []
        for v in m['volumes']:
            snapshot = next((s for s in snapshots if s['SnapshotId'] == v['snapshot_id']), None)
            if not snapshot or snapshot['VolumeId'] != v['volume_id'] or snapshot['OwnerId'] != self.c['account_id']:
                raise ValueError('Snapshot ownership/volume mismatch')
        self.wait_snapshots(m)
        return bundle

    def terraform(self, destroy=False):
        print('Planning disposable infrastructure:', 'destroy' if destroy else 'setup', flush=True)
        self.init_backend()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'operation.tfplan'
            command(['terraform', '-chdir=terraform', 'plan', '-input=false',
                     '-var=region='+self.c['region'], '-var=cluster_name='+self.c['cluster_name'],
                     *(['-destroy'] if destroy else []), '-out='+str(path)])
            plan = json.loads(command(['terraform', '-chdir=terraform', 'show', '-json', str(path)]))
            summary = {}
            for change in plan.get('resource_changes', []):
                action = '/'.join(change['change']['actions'])
                summary[action] = summary.get(action, 0)+1
            print('Infrastructure action counts:', json.dumps(summary), flush=True)
            if destroy:
                for change in plan.get('resource_changes', []):
                    if 'delete' in change['change']['actions']:
                        before = change['change'].get('before') or {}
                        if (change['type'] in ('aws_ecr_repository', 'aws_ebs_volume', 'aws_ebs_snapshot',
                                'aws_s3_bucket', 'aws_kms_key', 'aws_iam_openid_connect_provider') or
                            before.get('tags_all', {}).get('Lifecycle') == 'persistent' or
                            before.get('name') == self.c['cluster_name']+'-github-lifecycle'):
                            raise ValueError('Destroy plan includes a persistent resource')
            elif any('delete' in x['change']['actions'] for x in plan.get('resource_changes', [])):
                raise ValueError('Setup requires replacement/deletion; review manually')
            command(['terraform', '-chdir=terraform', 'apply', '-input=false', str(path)])

    def shutdown(self, backup_id):
        m = self.backup(backup_id)
        bundle = self.check_backup(m)
        self.restore_files(bundle['files'])
        try:
            self.eks.describe_cluster(name=self.c['cluster_name'])
        except self.eks.exceptions.ResourceNotFoundException:
            self.terraform(True)
            self.verify_destroyed(m)
            self.put('backups/'+backup_id+'/shutdown-complete.json', encoded({'backup_id': backup_id, 'status': 'destroyed'}))
            return
        self.kubeconfig()
        if m.get('cluster_uid') != json.loads(self.kube('get', 'namespace', 'kube-system', '-o', 'json'))['metadata']['uid']:
            raise ValueError('Backup belongs to a different cluster generation; create a new backup ID')
        # Suspend reconciliation before removing ingresses; never scale directory replicas.
        applications = json.loads(self.kube('-n', 'argocd', 'get', 'applications', '-o', 'json'))['items']
        for app in applications:
            self.kube('-n', 'argocd', 'patch', 'application', app['metadata']['name'], '--type=merge',
                      '-p', '{"spec":{"syncPolicy":{"automated":null}}}')
        present = {x['metadata']['name'] for x in json.loads(self.kube('get', 'namespaces', '-o', 'json'))['items']}
        for namespace in NAMESPACES:
            if namespace not in present:
                continue
            self.kube('-n', namespace, 'delete', 'ingress', '--all', '--ignore-not-found', '--wait=true')
        # Controller removes AWS load balancers asynchronously. Wait before losing it.
        import boto3
        elb = boto3.client('elbv2', region_name=self.c['region'])
        for _ in range(120):
            remaining = []
            for page in elb.get_paginator('describe_load_balancers').paginate():
                for lb in page['LoadBalancers']:
                    tags = elb.describe_tags(ResourceArns=[lb['LoadBalancerArn']])['TagDescriptions'][0]['Tags']
                    if any(t['Key'] == 'elbv2.k8s.aws/cluster' and t['Value'] == self.c['cluster_name'] for t in tags):
                        remaining.append(lb)
            if not remaining:
                break
            time.sleep(10)
        else:
            raise ValueError('Cluster load balancers remain; shutdown paused')
        self.terraform(True)
        self.verify_destroyed(m)
        self.put('backups/'+backup_id+'/shutdown-complete.json', encoded({'backup_id': backup_id, 'status': 'destroyed'}))
        print('Disposable infrastructure destroyed; volumes, snapshots and foundation retained.')

    def verify_destroyed(self, m):
        try:
            self.eks.describe_cluster(name=self.c['cluster_name'])
        except self.eks.exceptions.ResourceNotFoundException:
            pass
        else:
            raise ValueError('EKS cluster still exists')
        for page in self.ec2.get_paginator('describe_instances').paginate(Filters=[
                {'Name': 'tag:eks:cluster-name', 'Values': [self.c['cluster_name']]},
                {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped']} ]):
            if page['Reservations']:
                raise ValueError('Cluster worker instances remain')
        if self.ec2.describe_vpcs(Filters=[{'Name': 'tag:Project', 'Values': [self.c['cluster_name']]}])['Vpcs']:
            raise ValueError('Project VPC remains')
        for v in m['volumes']:
            volume = self.ec2.describe_volumes(VolumeIds=[v['volume_id']])['Volumes'][0]
            if volume['State'] != 'available':
                raise ValueError('Retained volume is not detached')
        self.wait_snapshots(m)
        import boto3
        ecr = boto3.client('ecr', region_name=self.c['region'])
        ecr.describe_repositories(repositoryNames=[self.c['cluster_name']+'/'+product for product in (
            'pingdirectory', 'pingdirectoryproxy', 'pingfederate', 'pingaccess')])
        for bucket in (self.c['state_bucket'], self.c['backup_bucket']):
            self.s3.head_bucket(Bucket=bucket)

    def restore_files(self, files):
        for name, value in files.items():
            path = (ROOT/name).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not (name in CONFIG_FILES or name.startswith('.secrets/')):
                raise ValueError('Unsafe backup path')
            data = base64.b64decode(value, validate=True)
            if path.exists() and path.read_bytes() != data:
                raise ValueError('Existing local file differs from selected backup: '+name)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data); path.chmod(0o600)

    def validate_restore_volume(self, volume, saved):
        if volume['AvailabilityZone'] != saved['az'] or not volume['Encrypted']:
            raise ValueError('Restored volume has incorrect AZ or encryption')
        if volume['State'] == 'in-use':
            # Resume only if this exact volume is already bound in the expected EKS
            # cluster and all attachments belong to its nodes. Never force detach.
            self.kubeconfig()
            pvs = json.loads(self.kube('get', 'pv', '-o', 'json'))['items']
            if not any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == volume['VolumeId'] for p in pvs):
                raise ValueError('Attached volume is not bound in the target cluster')
            nodes = json.loads(self.kube('get', 'nodes', '-o', 'json'))['items']
            instances = {n['spec'].get('providerID', '').rsplit('/', 1)[-1] for n in nodes}
            if not volume.get('Attachments') or any(a['InstanceId'] not in instances for a in volume['Attachments']):
                raise ValueError('Volume attachment belongs to another cluster')
            return
        if volume['State'] not in ('available', 'creating'):
            raise ValueError('Volume is not reusable')
        self.ec2.get_waiter('volume_available').wait(VolumeIds=[volume['VolumeId']])

    def verify_ldap(self):
        base_dn = json.loads((ROOT/'config.json').read_text())['base_dn']
        if not re.fullmatch(r'(dc|o)=[A-Za-z0-9-]+(,dc=[A-Za-z0-9-]+)*', base_dn):
            raise ValueError('Unexpected base DN')
        name = 'lifecycle-'+uuid.uuid4().hex
        dn = 'cn='+name+','+base_dn
        common = ['--noPropertiesFile', '--hostname', 'localhost', '--port', '1636', '--useSSL',
            '--trustStorePath', '/run/ping-secrets/truststore.p12', '--trustStorePasswordFile',
            '/run/ping-secrets/truststore.pin', '--trustStoreFormat', 'PKCS12', '--bindDN',
            'cn=administrator', '--bindPasswordFile', '/run/ping-secrets/root-user-password']
        proxy = ['-n', 'identity-proxy', 'exec', '-i', 'pd-pingdirectoryproxy-0', '-c', 'pingdirectoryproxy', '--', 'ldapmodify', *common]
        self.kube(*proxy, data=f'dn: {dn}\nchangetype: add\nobjectClass: organizationalRole\ncn: {name}\n\n')
        try:
            for colour in ('blue', 'green'):
                for index in range(3):
                    for _ in range(36):
                        try:
                            result = self.kube('-n', 'identity-'+colour, 'exec', f'pd-pingdirectory-{index}',
                                '-c', 'pingdirectory', '--', 'ldapsearch', *common, '--baseDN', dn,
                                '--searchScope', 'base', '(objectClass=*)', 'dn')
                            if dn not in result:
                                raise ValueError('Probe entry absent')
                            break
                        except ValueError:
                            time.sleep(5)
                    else:
                        raise ValueError('LDAP write was not observed on every Directory replica')
        finally:
            self.kube(*proxy, data=f'dn: {dn}\nchangetype: delete\n\n')

    def setup(self, backup_id, secret_source='vault', from_snapshots=False):
        print('Restoring backup:', backup_id, '; secrets:', secret_source, flush=True)
        m = json.loads(self.object('backups/'+backup_id+'/manifest.json'))
        bundle = self.check_backup(m)
        prepared, _ = self.prepare(bundle['inventories'])
        if secret_source == 'vault':
            vault = self.vault()
            try:
                prepared['20-runtime-secrets.yaml'] = read_secrets(vault, m['vault_versions'])
                files = vault.call('ping-eks/data/lab/bootstrap/files?version='+str(m['bootstrap_version']))['data']['data']['files']
                if files != bundle['files']:
                    raise ValueError('Bootstrap files differ from encrypted recovery bundle')
            finally:
                vault.close()
        # Validate original TLS identities before provisioning any infrastructure.
        prepared['20-runtime-secrets.yaml'].extend(argocd_tls_from_files(bundle['files']))
        self.restore_files(bundle['files'])
        # Explicit encrypted-backup mode uses independently preserved exact values.
        handles = {}
        required_handles = {doc['spec']['csi']['volumeHandle'] for doc in prepared['30-retained-claims.yaml']
                            if doc['kind'] == 'PersistentVolume'}
        for v in m['volumes']:
            if v['volume_id'] not in required_handles:
                continue
            if from_snapshots:
                existing = self.ec2.describe_volumes(Filters=[{'Name': 'tag:RestoreBackup', 'Values': [backup_id]},
                    {'Name': 'tag:OriginalVolume', 'Values': [v['volume_id']]}])['Volumes']
                if len(existing) > 1:
                    raise ValueError('Ambiguous snapshot restore')
                restored = existing[0] if existing else self.ec2.create_volume(SnapshotId=v['snapshot_id'],
                    AvailabilityZone=v['az'], VolumeType='gp3', Encrypted=True,
                    **({'KmsKeyId': v['kms_key_id']} if v.get('kms_key_id') else {}), TagSpecifications=[{'ResourceType': 'volume',
                    'Tags': [{'Key': 'Project', 'Value': self.c['cluster_name']}, {'Key': 'RestoreBackup', 'Value': backup_id},
                             {'Key': 'OriginalVolume', 'Value': v['volume_id']}, {'Key': 'ebs.csi.aws.com/cluster', 'Value': 'true'}]}])
                handles[v['volume_id']] = restored['VolumeId']
            else:
                restored = self.ec2.describe_volumes(VolumeIds=[v['volume_id']])['Volumes'][0]
                handles[v['volume_id']] = v['volume_id']
            self.validate_restore_volume(restored, v)
            self.ec2.create_tags(Resources=[handles[v['volume_id']]], Tags=[{'Key': 'ebs.csi.aws.com/cluster', 'Value': 'true'}])
        for doc in prepared['30-retained-claims.yaml']:
            if doc['kind'] == 'PersistentVolume':
                original = doc['spec']['csi']['volumeHandle']
                doc['spec']['csi']['volumeHandle'] = handles[original]
        self.terraform()
        self.kubeconfig()
        command([sys.executable, ROOT/'scripts/project.py', 'bootstrap'])
        self.bootstrap_runtime(prepared)
        project_doc = ROOT/'gitops/applications/project.yaml'
        self.kube('apply', '-f', str(project_doc))
        import yaml
        apps = {}
        for path in sorted((ROOT/'gitops/applications').glob('*.yaml')):
            for doc in yaml.safe_load_all(path.read_text()):
                if not doc or doc['kind'] not in ('Application', 'AppProject'):
                    continue
                if doc['kind'] == 'Application':
                    doc.get('spec', {}).get('syncPolicy', {}).pop('automated', None)
                    apps[doc['metadata']['name']] = doc
                self.kube('apply', '-f', '-', data=json.dumps(doc))
        seed = json.loads((ROOT/'config.json').read_text())['initial_seed_colour']
        order = [f'pd-{seed}', f'pd-{"green" if seed == "blue" else "blue"}', 'pd-proxy',
                 'pf-blue', 'pf-green', 'pa-blue', 'pa-green', 'logging', 'metrics', 'delegator', 'pingcentral']
        for name in order:
            if name not in apps:
                continue
            print('Syncing application:', name, flush=True)
            operation_id = str(uuid.uuid4())
            self.kube('-n', 'argocd', 'patch', 'application', name, '--type=merge',
                      '-p', json.dumps({'operation': {'sync': {'prune': False},
                          'info': [{'name': 'lifecycle-operation', 'value': operation_id}]}}))
            for _ in range(180):
                app = json.loads(self.kube('-n', 'argocd', 'get', 'application', name, '-o', 'json'))
                status = app.get('status', {})
                state = status.get('operationState', {})
                is_current = {'name': 'lifecycle-operation', 'value': operation_id} in state.get('operation', {}).get('info', [])
                if is_current and state.get('phase') in ('Failed', 'Error'):
                    raise ValueError('Argo sync failed: '+name)
                if (is_current and not app.get('operation') and state.get('phase') == 'Succeeded' and
                    status.get('sync', {}).get('status') == 'Synced' and status.get('health', {}).get('status') == 'Healthy'):
                    break
                time.sleep(10)
            else:
                raise ValueError('Argo readiness timed out: '+name)
        for args in self.c['verification_commands']:
            command(args)
        self.verify_ldap()
        # Public ingress routing is operator-owned and absent from workload exports.
        for inventory in bundle['inventories'].values():
            for doc in inventory['items']:
                if doc.get('kind') == 'Ingress':
                    metadata = doc['metadata']
                    clean = {'apiVersion': doc['apiVersion'], 'kind': 'Ingress', 'metadata': {
                        'name': metadata['name'], 'namespace': metadata['namespace'],
                        'labels': metadata.get('labels', {}), 'annotations': {
                            k: v for k, v in metadata.get('annotations', {}).items()
                            if k != 'kubectl.kubernetes.io/last-applied-configuration'}}, 'spec': doc['spec']}
                    self.kube('apply', '-f', '-', data=json.dumps(clean))
        self.put('backups/'+backup_id+'/restore-complete.json', encoded({'backup_id': backup_id,
            'volumes': handles, 'source': 'snapshots' if from_snapshots else 'original-volumes'}))
        print('Restore verified:', backup_id)

    def bootstrap_runtime(self, prepared):
        for key in ('00-namespaces.yaml', '20-runtime-secrets.yaml', '30-retained-claims.yaml'):
            for doc in prepared[key]:
                self.kube('apply', '-f', '-', data=json.dumps(doc))
        runtime = prepared['40-runtime-configmaps.yaml']
        for doc in runtime:
            if doc['metadata']['namespace'] != 'argocd':
                self.kube('apply', '-f', '-', data=json.dumps(doc))
        (ROOT/'.secrets/vault-managed').touch(mode=0o600)
        for script, args in [('federate.py', ['install-controller']), ('gitops.py', ['bootstrap'])]:
            command([sys.executable, ROOT/'scripts'/script, *args])
        # Helm creates its ConfigMaps first; restoring their data afterwards keeps
        # Helm ownership intact instead of conflicting with a pre-created resource.
        for doc in runtime:
            if doc['metadata']['namespace'] == 'argocd':
                self.kube('apply', '-f', '-', data=json.dumps(doc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'backup', 'adopt', 'shutdown', 'setup'])
    parser.add_argument('--config', type=Path, default=ROOT/'lifecycle.json')
    parser.add_argument('--backup-id', required=True)
    parser.add_argument('--secret-source', choices=['vault', 'encrypted-backup'], default='vault')
    parser.add_argument('--from-snapshots', action='store_true')
    parser.add_argument('--inventory', type=Path, help='Existing teardown inventory for initial adoption')
    args = parser.parse_args()
    c = validate_config(json.loads(args.config.read_text()))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.backup_id):
        raise ValueError('Invalid backup ID')
    if args.operation == 'plan':
        print(json.dumps({'account': c['account_id'], 'region': c['region'], 'cluster': c['cluster_name'],
            'backup_id': args.backup_id, 'retain_volumes': True, 'consistency': c['consistency'],
            'operations': ['backup', 'shutdown', 'setup'], 'cloud_changes': False}, indent=2))
        return
    lifecycle = Lifecycle(c)
    with lifecycle.lock():
        if args.operation == 'adopt':
            if not args.inventory or not args.inventory.is_dir():
                raise ValueError('Adoption requires an existing inventory directory')
            lifecycle.backup(args.backup_id, args.inventory)
        elif args.operation == 'setup':
            lifecycle.setup(args.backup_id, args.secret_source, args.from_snapshots)
        else:
            getattr(lifecycle, args.operation)(args.backup_id)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Lifecycle operation failed; no completion claimed. Check prerequisites and retry the same backup ID. Sensitive output suppressed.') from None
