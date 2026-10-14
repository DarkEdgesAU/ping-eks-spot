import datetime as dt
import json
from pathlib import Path
import sys
import unittest
import os
import subprocess
import tempfile
from unittest.mock import patch
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import federate
import project


class FederateTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((project.ROOT/'federate.example.json').read_text())

    def test_proxy_wait_retries_and_blocks_startup_on_exhaustion(self):
        # Exercise retry/error control flow without making LDAP/TLS requests.
        wrapper='''
COUNT=0
openssl() { printf '%s\\n' "$*" >> "$ATTEMPTS_FILE"; COUNT=$((COUNT + 1)); test "$COUNT" -gt "$FAIL_ATTEMPTS"; }
timeout() { shift; "$@"; }
sleep() { :; }
. "$WAIT_SCRIPT"
'''
        for failures,expected in ((2,0),(99,1)):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                ca=root/'ca.crt'; ca.write_text('fixture-only')
                attempts=root/'attempts.txt'
                env={**os.environ,'PROXY_CA_FILE':ca.as_posix(),'PROXY_WAIT_ATTEMPTS':'3',
                     'FAIL_ATTEMPTS':str(failures),'ATTEMPTS_FILE':attempts.as_posix(),
                     'WAIT_SCRIPT':(project.ROOT/'scripts/wait_for_directory_proxy.sh').as_posix()}
                result=subprocess.run([project.profile_shell(),'-c',wrapper],env=env,text=True,capture_output=True)
                self.assertEqual(result.returncode,expected,result.stderr)
                calls=attempts.read_text().splitlines()
                self.assertEqual(len(calls),3)
                self.assertIn('-verify_hostname pd-pingdirectoryproxy.identity-proxy.svc.cluster.local',calls[0])
                self.assertIn('-verify_return_error',calls[0])
                self.assertIn('reachable with trusted TLS' if expected==0 else 'blocking PF startup',result.stdout+result.stderr)

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
            self.assertEqual({x['metadata'].get('namespace',f'identity-federate-{colour}') for x in docs},
                             {f'identity-federate-{colour}'})
            self.assertFalse(any(x['kind']=='Ingress' or x['metadata']['name'].startswith('pf-active-') for x in docs))
            admin=next(x for x in docs if x['kind']=='StatefulSet')
            self.assertEqual(admin['spec']['replicas'],1)
            self.assertEqual(admin['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'],project.config()['storage_class'])
            engine=next(x for x in docs if x['kind']=='Deployment')
            self.assertEqual(engine['spec']['replicas'],3)
            pod=engine['spec']['template']['spec']
            for workload in (admin,engine):
                init=workload['spec']['template']['spec']['initContainers']
                names=[item['name'] for item in init]
                self.assertIn('wait-for-directory-proxy',names)
                self.assertLess(names.index('copy-federate-secrets'),names.index('wait-for-directory-proxy'))
            self.assertEqual({x['topologyKey'] for x in pod['topologySpreadConstraints']},
                {'topology.kubernetes.io/zone','kubernetes.io/hostname'})
            for constraint in pod['topologySpreadConstraints']:
                self.assertEqual(constraint['whenUnsatisfiable'],'DoNotSchedule')
                self.assertEqual(constraint['labelSelector']['matchLabels']['app.kubernetes.io/instance'],f'pf-{colour}')
            cm=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name'].endswith('engine-env-vars'))
            self.assertEqual(cm['data']['DNS_QUERY_LOCATION'],f'pf-{colour}-pingfederate-cluster.identity-federate-{colour}.svc.cluster.local')

    def test_routing_keeps_both_roles_on_selected_colour(self):
        config={**self.config,'admin_access_configured':True,'origin_certificate_arn':'arn:test',
                'cloudflare_ipv4_cidrs':['173.245.48.0/20']}
        for colour in project.COLOURS:
            ingresses=federate.routing(colour,config)['items']
            self.assertEqual(len(ingresses),4)
            for ingress in ingresses:
                active=ingress['metadata']['namespace']==federate.namespace(colour)
                annotations=ingress['metadata']['annotations']
                self.assertEqual(annotations['identity.darkedges.com/routing-state'],'active' if active else 'standby')
                backend=ingress['spec']['rules'][0]['http']['paths'][0]['backend']['service']
                self.assertEqual(annotations['alb.ingress.kubernetes.io/group.order'],'0' if active else '100')
                if active: self.assertTrue(backend['name'].startswith(f'pf-{colour}-'))
                else:
                    self.assertEqual(backend['name'],'pf-standby')
                    self.assertEqual(backend['port'],{'name':'use-annotation'})

    def test_partial_promotion_fails_closed_for_configuration(self):
        def active():
            return json.dumps({'metadata':{'annotations':{'identity.darkedges.com/routing-state':'active'}}})
        with patch.object(federate,'config',return_value=self.config),patch.object(project,'run',side_effect=[active(),active()]):
            with self.assertRaises(ValueError): federate.observed_colour()
        with patch.object(federate,'config',return_value=self.config),patch.object(project,'run',side_effect=['','']):
            self.assertIsNone(federate.observed_colour())

    def test_variable_projection_uses_regular_runtime_copy(self):
        v=federate.values(self.config,'blue')
        projected=v['volumes']['pf-projected']['secret']
        self.assertIn({'key':'PingFederate.lic','path':'pingfederate.lic'},projected['items'])
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
