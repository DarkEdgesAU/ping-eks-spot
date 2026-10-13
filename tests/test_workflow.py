import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('project', Path(__file__).resolve().parents[1]/'scripts/project.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class PromotionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.c = p.config()
        self.now = dt.datetime.now(dt.timezone.utc)
        self.e = {'cluster_name':self.c['cluster_name'],'target_colour':'green','image_tag':self.c['image_tag'],
                  'base_dn':self.c['base_dn'],'verified_at':self.now.isoformat(),'operator':'operator',
                  'evidence_notes':'Replication reports and write reconciliation checked.',
                  'replication_verified_all_base_dns':True,'writes_reconciled':True,
                  'rollback_compatible':True,'direct_writers_accounted_for':True}
    def test_valid_attestation(self):
        p.validate_evidence(self.e,self.c,'green',self.now)
    def test_stale_attestation_refused(self):
        self.e['verified_at'] = (self.now-dt.timedelta(minutes=6)).isoformat()
        with self.assertRaises(ValueError): p.validate_evidence(self.e,self.c,'green',self.now)
    def test_other_cluster_refused(self):
        self.e['cluster_name']='some-production-cluster'
        with self.assertRaises(ValueError): p.validate_evidence(self.e,self.c,'green',self.now)
    def test_different_candidate_image_refused(self):
        self.e['image_tag']='untested-image'
        with self.assertRaises(ValueError): p.validate_evidence(self.e,self.c,'green',self.now)
    def test_unverified_writes_refused(self):
        self.e['writes_reconciled']=False
        with self.assertRaises(ValueError): p.validate_evidence(self.e,self.c,'green',self.now)
    def test_future_attestation_refused(self):
        self.e['verified_at']=(self.now+dt.timedelta(minutes=2)).isoformat()
        with self.assertRaises(ValueError): p.validate_evidence(self.e,self.c,'green',self.now)
    def test_invalid_target_refused(self):
        with self.assertRaises(ValueError): p.values(self.c,'production')
    def test_green_uses_existing_seed_and_separate_namespace_dns(self):
        v=p.values(self.c,'green')['pingdirectory']
        self.assertEqual(v['envs']['K8S_SEED_CLUSTER'],'blue')
        self.assertIn('identity-green',v['envs']['K8S_POD_HOSTNAME_SUFFIX'])
    def test_proxy_reads_persisted_colour(self):
        v=p.values(self.c,'proxy')['pingdirectoryproxy']
        self.assertEqual(v['container']['env'][0]['valueFrom']['configMapKeyRef']['name'],'pd-routing')
    def test_runtime_secrets_do_not_cover_service_account_mount(self):
        for target in ('blue', 'green', 'proxy'):
            v = p.values(self.c, target)['global']
            mounts = {m['name']: m['mountPath'] for m in v['volumeMounts']}
            self.assertEqual(mounts['runtime-secret-files'], '/run/ping-secrets')
            self.assertEqual(v['envs']['SECRETS_DIR'], mounts['runtime-secret-files'])
            for key in ('KEYSTORE_FILE', 'KEYSTORE_PIN_FILE', 'TRUSTSTORE_FILE', 'TRUSTSTORE_PIN_FILE'):
                self.assertTrue(v['envs'][key].startswith(mounts['runtime-secret-files'] + '/'))
    def test_same_named_cluster_in_other_account_refused(self):
        out={'cluster_name':self.c['cluster_name'],'region':self.c['region'],
             'cluster_arn':'arn:aws:eks:ap-southeast-2:111111111111:cluster/pd-spot-dev'}
        with patch.object(p,'tf_outputs',return_value=out), patch.object(p,'run',return_value='arn:aws:eks:ap-southeast-2:222222222222:cluster/pd-spot-dev\n'):
            with self.assertRaises(ValueError): p.check_context(self.c)

if __name__ == '__main__': unittest.main()
