import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import recovery


class RecoveryTests(unittest.TestCase):
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
