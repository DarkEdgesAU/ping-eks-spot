import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import access


class AccessTests(unittest.TestCase):
    def setUp(self): self.config=access.config()

    def test_render_isolates_clusters_and_retains_admin_data(self):
        for colour in ('blue','green'):
            docs=list(yaml.safe_load_all((access.project.BUILD/f'pa-{colour}.manifests.yaml').read_text()))
            self.assertEqual({x['metadata']['namespace'] for x in docs},{f'identity-access-{colour}'})
            self.assertFalse(any(x['kind'] in ('Secret','Ingress') for x in docs))
            admin=next(x for x in docs if x['kind']=='StatefulSet')
            engine=next(x for x in docs if x['kind']=='Deployment')
            self.assertEqual(admin['spec']['replicas'],1)
            self.assertEqual(engine['spec']['replicas'],3)
            self.assertEqual(admin['spec']['persistentVolumeClaimRetentionPolicy'],{'whenDeleted':'Retain','whenScaled':'Retain'})
            self.assertEqual(len(engine['spec']['template']['spec']['topologySpreadConstraints']),2)
            waits=engine['spec']['template']['spec']['initContainers']
            self.assertIn(f'pa-{colour}-pingaccess-admin',str(waits))
            self.assertNotIn('pf-blue-pingfederate',str(waits))

    def test_standby_engine_is_blocked_but_fixed_admin_stays_on_colour(self):
        c={**self.config,'origin_certificate_arn':'arn:test','cloudflare_ipv4_cidrs':['173.245.48.0/20']}
        docs=access.routing(c,'blue')['items']
        self.assertEqual(len(docs),4)
        for doc in docs:
            green=doc['metadata']['namespace'].endswith('green')
            primary=doc['spec']['rules'][0]['http']['paths'][0]['backend']['service']
            if green: self.assertEqual(primary['name'],'pa-standby')
            if doc['metadata']['name']=='pa-admin':
                fixed=doc['spec']['rules'][1]
                self.assertEqual(doc['spec']['rules'][0]['host'],'pingaccess-admin.eks.darkedges.com')
                self.assertEqual(fixed['host'],f'pingaccess-{"green" if green else "blue"}-admin.eks.darkedges.com')
                self.assertEqual(fixed['http']['paths'][0]['backend']['service']['port']['number'],9000)
                self.assertIn('green' if green else 'blue',fixed['host'])
        self.assertNotIn('identity-federate',str(docs))

    def test_ingress_refuses_missing_tls(self):
        with self.assertRaises(ValueError): access.ingress({**self.config,'origin_certificate_arn':''},'admin','blue',True)

    def test_promotion_requires_fresh_complete_cluster_bound_evidence(self):
        c=self.config
        evidence={'cluster_arn':'arn:test:cluster','target_colour':'green','image_tag':c['image_tags']['green'],
                  'verified_at':dt.datetime.now(dt.timezone.utc).isoformat(),'operator':'tester','notes':'checked',
                  **{flag:True for flag in access.FLAGS}}
        with patch.object(access.project,'tf_outputs',return_value={'cluster_arn':'arn:test:cluster'}):
            access.validate_evidence(evidence,c,'green')
            for flag in access.FLAGS:
                with self.assertRaises(ValueError): access.validate_evidence({**evidence,flag:False},c,'green')
            with self.assertRaises(ValueError): access.validate_evidence({**evidence,'cluster_arn':'other'},c,'green')
            with self.assertRaises(ValueError):
                access.validate_evidence({**evidence,'verified_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=10)).isoformat()},c,'green')

    def test_ingress_preserves_observed_colour_and_refuses_split_routing(self):
        def observed(command,capture=False):
            colour='green' if 'identity-access-green' in command else 'blue'
            return json.dumps({'metadata':{'annotations':{'identity.darkedges.com/routing-state':'active' if colour=='green' else 'standby'}}})
        with patch.object(access.project,'run',side_effect=observed):
            self.assertEqual(access.observed_colour(self.config),'green')
        with patch.object(access.project,'run',return_value=json.dumps({'metadata':{'annotations':{'identity.darkedges.com/routing-state':'active'}}})):
            with self.assertRaises(ValueError): access.observed_colour(self.config)
