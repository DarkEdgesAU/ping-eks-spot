import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import federate
import federate_migration
import project


class MigrationTests(unittest.TestCase):
    def test_restored_binding_keeps_original_claim_and_volume_untouched(self):
        pv={'spec':{'capacity':{'storage':'10Gi'},'storageClassName':'pingdirectory-ebs',
            'claimRef':{'namespace':'identity-federate','name':'old','uid':'old-uid'},
            'csi':{'driver':'ebs.csi.aws.com','volumeHandle':'vol-1111'},
            'nodeAffinity':{'required':{'nodeSelectorTerms':[]}}}}
        pvc={'metadata':{'name':'out-dir-pf-blue-pingfederate-admin-0'},
             'spec':{'volumeName':'old-pv','storageClassName':'pingdirectory-ebs','accessModes':['ReadWriteOnce']}}
        manifests=federate_migration.restored_claims(pv,pvc,'blue','vol-2222')
        self.assertEqual(pv['spec']['csi']['volumeHandle'],'vol-1111')
        self.assertEqual(pvc['spec']['volumeName'],'old-pv')
        self.assertEqual(manifests[0]['spec']['csi']['volumeHandle'],'vol-2222')
        self.assertEqual(manifests[0]['spec']['persistentVolumeReclaimPolicy'],'Retain')
        self.assertEqual(manifests[0]['spec']['claimRef']['namespace'],'identity-federate-blue')
        self.assertNotIn('uid',manifests[0]['spec']['claimRef'])
        self.assertEqual(manifests[1]['metadata']['namespace'],'identity-federate-blue')

    def test_retained_master_keys_are_not_overwritten_by_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); server=root/'server'; staging=root/'staging'
            key=server/'server/default/data/pf.jwk'; key.parent.mkdir(parents=True)
            key.write_text('retained-rotated-keys')
            staged=staging/'instance/server/default/data/pf.jwk'; staged.parent.mkdir(parents=True)
            staged.write_text('obsolete-bootstrap-key')
            env={**os.environ,'SERVER_ROOT_DIR':server.as_posix(),'STAGING_DIR':staging.as_posix(),
                 'CONTAINER_ENV':(root/'env').as_posix(),'PF_ENGINE_PUBLIC_HOSTNAME':'id.eks.darkedges.com'}
            subprocess.run([project.profile_shell(),(project.ROOT/'profiles/pingfederate/hooks/03-build-run-plan.sh.post').as_posix()],
                           env=env,text=True,capture_output=True,check=True)
            self.assertEqual(key.read_text(),'retained-rotated-keys')
            self.assertFalse(staged.exists())
