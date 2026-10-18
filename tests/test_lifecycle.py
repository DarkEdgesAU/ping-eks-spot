import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import lifecycle as life
import vault_runtime as runtime


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
            'name': 'pd-runtime', 'namespace': 'identity-blue'}, 'type': 'Opaque',
            'data': {'password': base64.b64encode(b'original-bytes\r\n').decode()}}

    def test_import_preserves_encoding_with_cas(self):
        vault = Mock(); vault.call.side_effect = [ValueError('HTTP 404'), {'data': {'version': 1}}]
        records = runtime.import_secrets(vault, 'ping-eks', 'lab', [self.secret])
        self.assertEqual(vault.call.call_args.args[2], {'options': {'cas': 0}, 'data': self.secret})
        self.assertEqual(records[0]['version'], 1)

    def test_identical_import_does_not_rotate(self):
        vault = Mock(); vault.call.return_value = {'data': {'data': self.secret, 'metadata': {'version': 7}}}
        self.assertEqual(runtime.import_secrets(vault, 'ping-eks', 'lab', [self.secret])[0]['version'], 7)
        self.assertEqual(vault.call.call_count, 1)

    def test_changed_import_uses_current_cas(self):
        old = copy.deepcopy(self.secret); old['data'] = {}
        vault = Mock(); vault.call.side_effect = [{'data': {'data': old, 'metadata': {'version': 7}}}, {'data': {'version': 8}}]
        runtime.import_secrets(vault, 'ping-eks', 'lab', [self.secret])
        self.assertEqual(vault.call.call_args.args[2]['options']['cas'], 7)

    def test_denied_access_is_not_missing(self):
        vault = Mock(); vault.call.side_effect = ValueError('HTTP 403')
        with self.assertRaises(ValueError): runtime.import_secrets(vault, 'ping-eks', 'lab', [self.secret])
        self.assertEqual(vault.call.call_count, 1)

    def test_exact_version_and_identity(self):
        vault = Mock(); vault.call.return_value = {'data': {'data': self.secret, 'metadata': {'version': 3}}}
        record = {'mount': 'ping-eks', 'path': 'lab/runtime/identity-blue/pd-runtime', 'version': 3,
                  'namespace': 'identity-blue', 'name': 'pd-runtime'}
        self.assertEqual(runtime.read_secrets(vault, [record]), [self.secret])
        self.assertTrue(vault.call.call_args.args[0].endswith('?version=3'))
        for key, value in [('name', 'wrong'), ('version', 'latest')]:
            with self.assertRaises(ValueError): runtime.read_secrets(vault, [dict(record, **{key: value})])

    def test_bad_encoding_rejected(self):
        self.secret['data']['password'] = '!'
        with self.assertRaises(ValueError): runtime.validate_secret(self.secret)

    def test_http_only_for_selected_local_endpoint(self):
        with self.assertRaises(ValueError): runtime.Vault('http://public.example:8200')
        runtime.Vault('http://vault.hashicorp-vault.svc.cluster.local:8200')

    def test_generation_guard(self):
        with patch.dict('os.environ', {'PING_SECRET_SOURCE': 'vault'}):
            with self.assertRaises(ValueError): runtime.generator_guard()

    def test_bootstrap_keys_are_versioned_without_regeneration(self):
        vault = Mock(); vault.call.side_effect = [ValueError('HTTP 404'), {'data': {'version': 2}}]
        files = {'.secrets/ca.key': base64.b64encode(b'original-key').decode()}
        self.assertEqual(runtime.import_bootstrap_files(vault, files), 2)
        self.assertEqual(vault.call.call_args.args[2]['data'], {'files': files})


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((life.ROOT/'lifecycle.example.json').read_text())
        self.c.update(state_bucket='state', backup_bucket='backups', kms_key_arn='arn:aws:kms:test')
        self.runner = life.Lifecycle.__new__(life.Lifecycle); self.runner.c = self.c

    def test_configuration_gates(self):
        for key, value in [('state_bucket', 'REPLACE'), ('account_id', 'wrong'), ('verification_commands', []),
                           ('consistency', 'application-consistent')]:
            with self.assertRaises(ValueError): life.validate_config(dict(self.c, **{key: value}))

    def test_complete_matching_manifest_required(self):
        m = {'schema': 1, 'status': 'complete', 'backup_id': 'test-01', 'volumes': [],
             **{k: self.c[k] for k in ('account_id', 'region', 'cluster_name')}}
        life.validate_manifest(m, self.c)
        for key, value in [('status', 'pending'), ('account_id', 'wrong'), ('backup_id', '../escape')]:
            with self.assertRaises(ValueError): life.validate_manifest(dict(m, **{key: value}), self.c)

    def test_envelope_authentication_and_tampering(self):
        kms = Mock(); kms.generate_data_key.return_value = {'Plaintext': b'x'*32, 'CiphertextBlob': b'wrapped'}
        kms.decrypt.return_value = {'Plaintext': b'x'*32}
        raw = life.envelope_encrypt(kms, 'key', {'private': 'value'}, 'backup-01')
        self.assertNotIn(b'value', raw)
        self.assertEqual(life.envelope_decrypt(kms, 'key', raw, 'backup-01'), {'private': 'value'})
        with self.assertRaises(Exception): life.envelope_decrypt(kms, 'key', raw, 'different-id')
        damaged = json.loads(raw); damaged['ciphertext'] = base64.b64encode(b'bad').decode()
        with self.assertRaises(Exception): life.envelope_decrypt(kms, 'key', json.dumps(damaged), 'backup-01')

    def test_failed_backup_or_verification_blocks_destroy(self):
        for failing in ('backup', 'check_backup'):
            self.runner.backup = Mock(return_value={}); self.runner.check_backup = Mock()
            setattr(self.runner, failing, Mock(side_effect=ValueError('failed')))
            self.runner.terraform = Mock()
            with self.assertRaises(ValueError): self.runner.shutdown('test-01')
            self.runner.terraform.assert_not_called()

    def test_restore_rejects_traversal_and_conflicting_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(life, 'ROOT', Path(directory)):
            data = base64.b64encode(b'original').decode()
            with self.assertRaises(ValueError): self.runner.restore_files({'../escape': data})
            self.runner.restore_files({'.secrets/ca.key': data})
            self.assertEqual((Path(directory)/'.secrets/ca.key').read_bytes(), b'original')
            with self.assertRaises(ValueError):
                self.runner.restore_files({'.secrets/ca.key': base64.b64encode(b'changed').decode()})

    def test_plan_refuses_persistent_deletion_and_setup_replacement(self):
        self.runner.init_backend = Mock()
        for destroy, resource in [(True, 'aws_ecr_repository'), (False, 'aws_eks_cluster')]:
            change = {'type': resource, 'change': {'actions': ['delete', 'create']}}
            with patch.object(life, 'command', side_effect=['', json.dumps({'resource_changes': [change]})]) as command:
                with self.assertRaises(ValueError): self.runner.terraform(destroy)
            self.assertEqual(command.call_count, 2)

    def test_lock_released_on_failure(self):
        self.runner.put = Mock(return_value={'ETag': 'owned-lock'}); self.runner.s3 = Mock()
        with self.assertRaises(ValueError):
            with self.runner.lock(): raise ValueError('failure')
        self.runner.s3.delete_object.assert_called_once()
        self.assertEqual(self.runner.s3.delete_object.call_args.kwargs['IfMatch'], 'owned-lock')

    def test_restore_never_force_detaches_an_unrelated_volume(self):
        self.runner.kubeconfig = Mock()
        self.runner.kube = Mock(return_value=json.dumps({'items': []}))
        volume = {'AvailabilityZone': 'ap-southeast-2a', 'Encrypted': True, 'State': 'in-use', 'VolumeId': 'vol-1'}
        with self.assertRaises(ValueError): self.runner.validate_restore_volume(volume, {'az': 'ap-southeast-2a'})

    def test_resume_accepts_only_target_cluster_attachment(self):
        self.runner.kubeconfig = Mock()
        self.runner.kube = Mock(side_effect=[json.dumps({'items': [{'spec': {'csi': {'volumeHandle': 'vol-1'}}}]}),
            json.dumps({'items': [{'spec': {'providerID': 'aws:///ap-southeast-2a/i-1'}}]})])
        volume = {'AvailabilityZone': 'ap-southeast-2a', 'Encrypted': True, 'State': 'in-use',
                  'VolumeId': 'vol-1', 'Attachments': [{'InstanceId': 'i-1'}]}
        self.runner.validate_restore_volume(volume, {'az': 'ap-southeast-2a'})

    def test_restored_az_and_encryption_are_verified(self):
        for volume in [{'AvailabilityZone': 'ap-southeast-2b', 'Encrypted': True},
                       {'AvailabilityZone': 'ap-southeast-2a', 'Encrypted': False}]:
            with self.assertRaises(ValueError): self.runner.validate_restore_volume(volume, {'az': 'ap-southeast-2a'})

    def inventory(self):
        docs = {ns+'-resources.json': {'items': []} for ns in (*life.recovery.CORE_NAMESPACES, 'argocd')}
        pvs = []
        for ns in ('identity-blue', 'identity-green', 'identity-proxy'):
            docs[ns+'-resources.json']['items'].append({'apiVersion': 'v1', 'kind': 'Secret',
                'metadata': {'namespace': ns, 'name': 'pd-runtime'}, 'data': {}})
        for ns in ('identity-blue', 'identity-green'):
            for i in range(3):
                name = ns+'-'+str(i)
                pvs.append({'metadata': {'name': name}, 'spec': {'csi': {
                    'driver': 'ebs.csi.aws.com', 'volumeHandle': 'vol-'+name}, 'claimRef': {'name': name, 'namespace': ns}}})
                docs[ns+'-resources.json']['items'].append({'kind': 'PersistentVolumeClaim',
                    'metadata': {'name': name, 'namespace': ns}, 'spec': {'volumeName': name}})
        docs['persistent-volumes.json'] = {'items': pvs}
        docs['identity-proxy-resources.json']['items'].append({'kind': 'ConfigMap',
            'metadata': {'name': 'pd-routing', 'namespace': 'identity-proxy'}, 'data': {'active_colour': 'green'}})
        return docs

    def test_incomplete_inventory_is_rejected_before_backup(self):
        docs = self.inventory(); life.validate_inventory(docs)
        docs['identity-blue-resources.json']['items'] = []
        with self.assertRaises(ValueError): life.validate_inventory(docs)

    def test_optional_secret_reference_does_not_block_historical_restore(self):
        docs = self.inventory()
        secret_ref = {'secretName': 'optional-dex-tls', 'optional': True}
        docs['argocd-resources.json']['items'].append({'kind': 'Deployment', 'metadata': {'name': 'argocd-server'},
            'spec': {'template': {'spec': {'volumes': [{'name': 'tls', 'secret': secret_ref}]}}}})
        life.validate_inventory(docs)
        secret_ref['optional'] = False
        with self.assertRaises(ValueError): life.validate_inventory(docs)

    def test_failed_snapshot_retry_reuses_frozen_bundle_and_snapshots(self):
        class Missing(Exception): pass
        store = {}; snapshots = {}
        self.runner.s3 = Mock(); self.runner.s3.exceptions.NoSuchKey = Missing
        def get(key):
            if key not in store: raise Missing()
            return store[key]
        self.runner.object = get
        self.runner.put = lambda key, raw, *args: store.update({key: raw})
        self.runner.kms = Mock()
        self.runner.kms.generate_data_key.return_value = {'Plaintext': b'x'*32, 'CiphertextBlob': b'wrapped'}
        self.runner.kms.decrypt.return_value = {'Plaintext': b'x'*32}
        docs = self.inventory()
        self.runner.inventory = Mock(return_value=docs)
        self.runner.hydrate_bootstrap = Mock()
        self.runner.kubeconfig = Mock(); self.runner.kube = Mock(return_value='{"metadata":{"uid":"cluster-1"}}')
        self.runner.local_files = Mock(return_value={}); self.runner.vault = Mock(return_value=Mock())
        self.runner.ec2 = Mock()
        self.runner.ec2.describe_volumes.return_value = {'Volumes': [{'Encrypted': True, 'AvailabilityZone': 'ap-southeast-2a', 'Size': 10}]}
        def existing(**kwargs):
            volume = next(f['Values'][0] for f in kwargs['Filters'] if f['Name'] == 'volume-id')
            return {'Snapshots': [snapshots[volume]] if volume in snapshots else []}
        def create(**kwargs):
            volume = kwargs['VolumeId']; snapshots[volume] = {'SnapshotId': 'snap-'+volume}
            return snapshots[volume]
        self.runner.ec2.describe_snapshots.side_effect = existing
        self.runner.ec2.create_snapshot.side_effect = create
        self.runner.wait_snapshots = Mock(side_effect=[ValueError('snapshot pending'), None])
        self.runner.check_backup = Mock()
        secrets = self.runner.prepare(docs)[0]['20-runtime-secrets.yaml']
        with patch.object(life, 'import_secrets', return_value=[]) as imported, \
             patch.object(life, 'read_secrets', return_value=secrets), \
             patch.object(life, 'import_bootstrap_files', return_value=1):
            with self.assertRaises(ValueError): self.runner.backup('retry-01')
            frozen = store['backups/retry-01/bundle.json.enc']
            self.assertNotIn('backups/retry-01/manifest.json', store)
            self.runner.backup('retry-01')
            imported.assert_called_once()
        self.assertEqual(store['backups/retry-01/bundle.json.enc'], frozen)
        self.assertEqual(self.runner.ec2.create_snapshot.call_count, 6)

    def test_fresh_runner_restores_bootstrap_before_backup(self):
        vault = Mock()
        files = {name: base64.b64encode(b'original').decode() for name in (
            'config.json', 'terraform/terraform.tfvars', '.secrets/ca.key', '.secrets/ca.crt')}
        vault.call.return_value = {'data': {'data': {'files': files}}}
        self.runner.vault = Mock(return_value=vault)
        self.runner.local_files = Mock(return_value={})
        self.runner.restore_files = Mock()
        self.runner.hydrate_bootstrap()
        self.runner.restore_files.assert_called_once_with(files)
        vault.close.assert_called_once()

    def test_missing_bootstrap_keys_cannot_be_replaced_by_empty_checkout(self):
        vault = Mock(); vault.call.return_value = {'data': {'data': {'files': {}}}}
        self.runner.vault = Mock(return_value=vault)
        self.runner.local_files = Mock(return_value={})
        self.runner.restore_files = Mock()
        with self.assertRaises(ValueError): self.runner.hydrate_bootstrap()
        self.runner.restore_files.assert_not_called()


if __name__ == '__main__': unittest.main()
