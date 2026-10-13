import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import federate
import project


class FederateTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((project.ROOT/'federate.example.json').read_text())

    def evidence(self):
        return {'cluster_name':project.config()['cluster_name'],'target_colour':'green',
            'image_tag':self.config['image_tags']['green'],
            'verified_at':dt.datetime.now(dt.timezone.utc).isoformat(),'operator':'test','notes':'verified',
            **{flag:True for flag in federate.EVIDENCE_FLAGS}}

    def test_promotion_requires_every_attestation_and_matching_identity(self):
        federate.validate_evidence(self.evidence(),self.config,'green')
        for flag in federate.EVIDENCE_FLAGS:
            evidence=self.evidence(); evidence[flag]=False
            with self.assertRaises(ValueError): federate.validate_evidence(evidence,self.config,'green')
        for field,value in (('target_colour','blue'),('cluster_name','other'),('image_tag','old')):
            evidence=self.evidence(); evidence[field]=value
            with self.assertRaises(ValueError): federate.validate_evidence(evidence,self.config,'green')

    def test_stale_evidence_rejected(self):
        evidence=self.evidence()
        evidence['verified_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=6)).isoformat()
        with self.assertRaises(ValueError): federate.validate_evidence(evidence,self.config,'green')

    def test_ingress_requires_origin_tls_and_admin_access(self):
        with self.assertRaises(ValueError): federate.ingress(self.config,'engine')
        self.config.update(origin_certificate_arn='arn:aws:acm:ap-southeast-2:123456789012:certificate/test',
                           cloudflare_ipv4_cidrs=['173.245.48.0/20'])
        engine=federate.ingress(self.config,'engine')
        self.assertEqual(engine['spec']['rules'][0]['host'],'id.eks.darkedges.com')
        self.assertEqual(engine['metadata']['annotations']['alb.ingress.kubernetes.io/backend-protocol'],'HTTPS')
        with self.assertRaises(ValueError): federate.ingress(self.config,'admin')
        self.config['admin_access_configured']=True
        self.assertEqual(federate.ingress(self.config,'admin')['spec']['rules'][0]['host'],'admin.eks.darkedges.com')

    def test_candidate_render_has_no_active_routing_or_ingress(self):
        for colour in project.COLOURS:
            docs=[x for x in yaml.safe_load_all((project.BUILD/f'pf-{colour}.manifests.yaml').read_text()) if x]
            self.assertFalse(any(x['kind']=='Ingress' or x['metadata']['name'].startswith('pf-active-') for x in docs))
            admin=next(x for x in docs if x['kind']=='StatefulSet')
            self.assertEqual(admin['spec']['replicas'],1)
            self.assertEqual(admin['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'],project.config()['storage_class'])
            engine=next(x for x in docs if x['kind']=='Deployment')
            self.assertEqual(engine['spec']['replicas'],3)
            pod=engine['spec']['template']['spec']
            self.assertEqual({x['topologyKey'] for x in pod['topologySpreadConstraints']},
                {'topology.kubernetes.io/zone','kubernetes.io/hostname'})
            for constraint in pod['topologySpreadConstraints']:
                self.assertEqual(constraint['whenUnsatisfiable'],'DoNotSchedule')
                self.assertEqual(constraint['labelSelector']['matchLabels']['app.kubernetes.io/instance'],f'pf-{colour}')
            cm=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name'].endswith('engine-env-vars'))
            self.assertEqual(cm['data']['DNS_QUERY_LOCATION'],f'pf-{colour}-pingfederate-cluster.identity-federate.svc.cluster.local')

    def test_routing_keeps_both_roles_on_selected_colour(self):
        for colour in project.COLOURS:
            services=federate.routing(colour)['items']
            self.assertEqual({x['spec']['selector']['app.kubernetes.io/instance'] for x in services},{f'pf-{colour}'})

    def test_partial_promotion_fails_closed_for_configuration(self):
        def service(colour):
            return json.dumps({'spec':{'selector':{'app.kubernetes.io/instance':f'pf-{colour}'}}})
        with patch.object(project,'run',side_effect=[service('blue'),service('green')]):
            with self.assertRaises(ValueError): federate.observed_colour()
        with patch.object(project,'run',side_effect=['','']):
            self.assertIsNone(federate.observed_colour())

    def test_variable_projection_uses_regular_runtime_copy(self):
        v=federate.values(self.config,'blue')
        projected=v['volumes']['pf-projected']['secret']
        self.assertIn({'key':'var.PF_CLUSTER_PASSWORD','path':'vars/PF_CLUSTER_PASSWORD'},projected['items'])
        self.assertEqual(v['volumes']['pf-secrets']['emptyDir']['medium'],'Memory')
        self.assertFalse(any(m['name']=='pf-projected' for m in v['global']['volumeMounts']))

    def test_profile_has_no_demo_secrets_and_ldap_uses_verified_tls(self):
        profile=project.ROOT/'profiles/pingfederate'
        self.assertFalse(list(profile.rglob('*.jwk')))
        self.assertFalse(list(profile.rglob('*.p12')))
        bulk=json.loads((profile/'instance/bulk-config/data.json.subst').read_text())
        store=next(i for o in bulk['operations'] if o['resourceType']=='/dataStores' for i in o['items'] if i.get('type')=='LDAP')
        self.assertTrue(store['useSsl']); self.assertTrue(store['verifyHost'])
        self.assertEqual(store['userDN'],'uid=pingfederate,ou=services,${USER_BASE_DN}')
        self.assertTrue(store['password'].startswith('${'))
        for path in (project.ROOT/'profiles').rglob('*'):
            if path.is_file():
                self.assertNotIn('2FederateM0re',path.read_text())
                self.assertNotIn('BEGIN PRIVATE KEY',path.read_text())
