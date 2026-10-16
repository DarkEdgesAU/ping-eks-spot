import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import platform_services as ps


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ps.SOURCE/'config.example.json').read_text())

    def test_optional_apps_start_disabled_and_central_data_is_retained(self):
        with patch.object(ps, 'config', return_value=self.c):
            docs = ps.metrics(self.c)+ps.ping_workload(self.c, 'pingcentral')+ps.ping_workload(self.c, 'delegator')
        self.assertNotIn('Secret', [d['kind'] for d in docs])
        for doc in docs:
            if doc['kind'] in ('StatefulSet', 'Deployment'):
                self.assertEqual(doc['spec']['replicas'], 0)
            if doc['kind'] == 'StatefulSet':
                self.assertEqual(doc['spec']['persistentVolumeClaimRetentionPolicy'], {'whenDeleted': 'Retain', 'whenScaled': 'Retain'})

    def test_metrics_preserves_directory_replica_count_and_volumes(self):
        original = [{'kind': 'StatefulSet', 'metadata': {'name': 'pd', 'namespace': 'identity-blue'},
            'spec': {'replicas': 3, 'template': {'spec': {'containers': [{'name': 'pingdirectory', 'volumeMounts': [{'name': 'data', 'mountPath': '/opt/out'}]}]}}}}]
        before = copy.deepcopy(original)
        with patch.object(ps, 'config', return_value=self.c): docs = ps.attach(original, enabled=True)
        self.assertEqual(original, before)
        self.assertEqual(docs[0]['spec']['replicas'], 3)
        pod = docs[0]['spec']['template']['spec']
        self.assertEqual(pod['containers'][0], before[0]['spec']['template']['spec']['containers'][0])
        self.assertEqual(pod['containers'][1]['name'], 'ping-metrics')
        self.assertNotIn('hostPath', str(pod))

    def test_discovery_has_no_secret_or_cluster_permissions(self):
        docs = ps.metrics(self.c)
        self.assertFalse(any(d['kind'].startswith('Cluster') for d in docs))
        for d in docs:
            if d['kind'] == 'Role':
                self.assertEqual(d['rules'][0]['resources'], ['pods'])
                self.assertEqual(d['rules'][0]['verbs'], ['get', 'list', 'watch'])

    def test_public_admin_requires_changed_password_and_delegator_verification(self):
        c = {**self.c, 'central_enabled': True, 'delegator_enabled': True}
        g = {'origin_certificate_arn': 'arn:example', 'cloudflare_ipv4_cidrs': ['173.245.48.0/20']}
        self.assertEqual(ps.ingresses(c, g), [])
        c.update(central_password_changed=True, central_tls_verified=True, delegator_integration_verified=True)
        docs = ps.ingresses(c, g)
        self.assertEqual(len(docs), 2)
        for d in docs:
            self.assertEqual(d['metadata']['annotations']['alb.ingress.kubernetes.io/backend-protocol'], 'HTTPS')
        with self.assertRaises(ValueError): ps.ingresses(c, {**g, 'cloudflare_ipv4_cidrs': ['0.0.0.0/0']})

    def test_delegator_browser_has_no_secret_and_api_checks_directory_tls(self):
        profile = ps.ping_workload(self.c, 'delegator')[0]['data']
        self.assertIn('AUTHENTICATE_WITH_PKCE = true', profile['profile-0'])
        self.assertNotIn('client_secret', profile['profile-0'])
        self.assertIn('proxy_ssl_verify on', profile['profile-1'])
        self.assertIn('identity-blue.svc.cluster.local', profile['profile-1'])

    def test_backend_change_triggers_rollout_and_profile_checksum_is_stable(self):
        blue = ps.ping_workload(self.c, 'delegator')[1]['spec']['template']
        repeated = ps.ping_workload(copy.deepcopy(self.c), 'delegator')[1]['spec']['template']
        green = ps.ping_workload({**self.c, 'directory_colour': 'green'}, 'delegator')[1]['spec']['template']
        self.assertEqual(blue, repeated)
        self.assertNotEqual(blue['metadata']['annotations']['checksum/profile'],
                            green['metadata']['annotations']['checksum/profile'])
        self.assertEqual(blue['spec'], green['spec'])
        # Central uses the same profile-copy lifecycle, and needs the same guard.
        central = ps.ping_workload(self.c, 'pingcentral')[1]['spec']['template']
        self.assertIn('checksum/profile', central['metadata']['annotations'])

    def test_generated_client_separates_baseline_common_and_exclusive_scopes(self):
        baseline = json.loads((ps.ROOT/'profiles/pingfederate/instance/bulk-config/data.json.subst').read_text())
        exclusive = {scope['name'] for op in baseline['operations'] for item in op.get('items', [])
                     for scope in item.get('exclusiveScopes', []) if isinstance(scope, dict)}
        with tempfile.TemporaryDirectory() as folder, patch.object(ps.project, 'BUILD', Path(folder)):
            ps.integration_plan(self.c)
            client = json.loads((Path(folder)/'delegator-pf-client.json').read_text())
        self.assertEqual(client['restrictedScopes'], ['openid'])
        self.assertEqual(client['exclusiveScopes'], ['urn:pingidentity:directory-delegated-admin'])
        self.assertTrue(set(client['exclusiveScopes']).issubset(exclusive))
        self.assertTrue(set(client['restrictedScopes']).isdisjoint(exclusive))
        self.assertTrue(client['requireProofKeyForCodeExchange'])


if __name__ == '__main__': unittest.main()
