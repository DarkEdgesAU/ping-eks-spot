import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import recovery


class RecoveryTests(unittest.TestCase):
    def inventory(self, folder, namespaces):
        for ns in namespaces:
            (folder/f'{ns}-resources.json').write_text(json.dumps({'items': []}))
        (folder/'persistent-volumes.json').write_text(json.dumps({'items': []}))

    def test_historical_backup_without_optional_services_remains_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inventory(root, (*recovery.CORE_NAMESPACES, 'argocd'))
            docs, volumes = recovery.prepare(root)
        self.assertEqual(volumes, [])
        self.assertEqual({d['metadata']['name'] for d in docs['00-namespaces.yaml']},
                         set(recovery.CORE_NAMESPACES) | {'argocd'})

    def test_optional_state_and_original_secrets_are_recovered(self):
        targets = ['identity-access-blue', 'identity-access-green', 'identity-central', 'logging', 'monitoring']
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inventory(root, (*recovery.CORE_NAMESPACES, *targets, 'argocd'))
            pvs = []
            for i, ns in enumerate(targets):
                pv = {'metadata': {'name': f'pv-{i}'}, 'spec': {
                    'csi': {'driver': 'ebs.csi.aws.com', 'volumeHandle': f'vol-{i}'},
                    'claimRef': {'name': 'data', 'namespace': ns, 'uid': 'old'},
                    'nodeAffinity': {'required': {'nodeSelectorTerms': [{'matchExpressions': [
                        {'key': 'topology.kubernetes.io/zone', 'operator': 'In', 'values': ['ap-southeast-2b']}]}]}}}}
                pvs.append(pv)
                (root/f'{ns}-resources.json').write_text(json.dumps({'items': [
                    {'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'data', 'namespace': ns},
                     'spec': {'volumeName': f'pv-{i}', 'accessModes': ['ReadWriteOnce']}},
                    {'kind': 'Secret', 'metadata': {'name': 'runtime', 'namespace': ns},
                     'data': {'password': 'b3JpZ2luYWw='}}]}))
            (root/'persistent-volumes.json').write_text(json.dumps({'items': pvs}))
            docs, volumes = recovery.prepare(root)
        self.assertEqual(set(volumes), {f'vol-{i}' for i in range(len(targets))})
        self.assertEqual({d['metadata']['namespace'] for d in docs['20-runtime-secrets.yaml']}, set(targets))
        for d in docs['20-runtime-secrets.yaml']:
            self.assertEqual(d['data']['password'], 'b3JpZ2luYWw=')
        restored_pvs = [d for d in docs['30-retained-claims.yaml'] if d['kind'] == 'PersistentVolume']
        self.assertEqual(len(restored_pvs), len(targets))
        for d in restored_pvs:
            self.assertEqual(d['spec']['persistentVolumeReclaimPolicy'], 'Retain')
            self.assertEqual(d['spec']['nodeAffinity'], pvs[0]['spec']['nodeAffinity'])

    def test_optional_namespace_inventory_cannot_be_skipped_when_a_pv_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inventory(root, (*recovery.CORE_NAMESPACES, 'argocd'))
            (root/'persistent-volumes.json').write_text(json.dumps({'items': [
                {'spec': {'claimRef': {'namespace': 'identity-central'}}}]}))
            with self.assertRaisesRegex(ValueError, 'Missing namespace inventory: identity-central'):
                recovery.prepare(root)

    def test_retained_binding_strips_old_cluster_identity_without_changing_disk_or_zone(self):
        pv={'metadata':{'name':'pv-one','uid':'old-uid','finalizers':['old']},'spec':{
            'csi':{'driver':'ebs.csi.aws.com','volumeHandle':'vol-original'},
            'claimRef':{'name':'out-dir','namespace':'identity-blue','uid':'old-claim','resourceVersion':'12'},
            'persistentVolumeReclaimPolicy':'Delete','nodeAffinity':{'required':{'nodeSelectorTerms':[]}}}}
        pvc={'metadata':{'name':'out-dir','namespace':'identity-blue','uid':'old'},'spec':{'volumeName':'pv-one'}}
        before=copy.deepcopy(pv)
        restored,claim=recovery.binding(pv,pvc)
        self.assertEqual(pv,before)
        self.assertEqual(restored['spec']['csi']['volumeHandle'],'vol-original')
        self.assertEqual(restored['spec']['nodeAffinity'],pv['spec']['nodeAffinity'])
        self.assertEqual(restored['spec']['persistentVolumeReclaimPolicy'],'Retain')
        self.assertEqual(restored['spec']['claimRef'],{'name':'out-dir','namespace':'identity-blue'})
        self.assertEqual(restored['metadata'],{'name':'pv-one'})
        self.assertNotIn('uid',claim['metadata'])

    def test_secret_preserves_data_but_removes_old_owner_references(self):
        secret={'kind':'Secret','metadata':{'name':'runtime','namespace':'identity-blue','ownerReferences':[{}],
            'resourceVersion':'12'},'type':'Opaque','data':{'password':'cHJpdmF0ZQ=='}}
        restored=recovery.clean_secret(secret)
        self.assertEqual(restored['data'],secret['data'])
        self.assertNotIn('ownerReferences',restored['metadata'])
        for kind in ('helm.sh/release.v1','kubernetes.io/service-account-token'):
            self.assertIsNone(recovery.clean_secret({**secret,'type':kind}))
        self.assertIsNone(recovery.clean_secret({**secret,'metadata':{
            'namespace':'argocd','name':'argocd-redis'}}))

    def test_binding_refuses_mismatched_claim(self):
        with self.assertRaises(ValueError):
            recovery.binding({'spec':{'csi':{'driver':'ebs.csi.aws.com'}},'metadata':{'name':'one'}},
                             {'spec':{'volumeName':'another'}})
