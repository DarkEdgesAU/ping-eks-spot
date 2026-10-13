import copy
import json
from pathlib import Path
import sys
import unittest
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import gitops
import project


class GitOpsTests(unittest.TestCase):
    def setUp(self): self.config=gitops.config()

    def test_apps_are_manual_and_do_not_cascade_delete(self):
        for name,ns in gitops.APPS.items():
            app=gitops.application(self.config,name,ns)
            self.assertNotIn('finalizers',app['metadata'])
            self.assertNotIn('automated',app['spec']['syncPolicy'])
            self.assertIn('RespectIgnoreDifferences=true',app['spec']['syncPolicy']['syncOptions'])
            self.assertIn('/spec/replicas',app['spec']['ignoreDifferences'][0]['jsonPointers'])

    def test_exports_contain_no_secrets_or_runtime_routing(self):
        for name in gitops.APPS:
            path=gitops.GITOPS/'workloads'/name/'resources.yaml'
            self.assertTrue(path.is_file())
            for doc in yaml.safe_load_all(path.read_text()):
                if not doc: continue
                self.assertNotEqual(doc['kind'],'Secret')
                self.assertNotIn(doc['metadata']['name'],('pd-routing','pf-active-admin','pf-active-engine'))
                if doc['kind']=='StatefulSet':
                    self.assertEqual(doc['metadata']['annotations']['argocd.argoproj.io/sync-options'],'Prune=false,Delete=false')
                    self.assertEqual(doc['spec']['persistentVolumeClaimRetentionPolicy'],{'whenDeleted':'Retain','whenScaled':'Retain'})
                    if doc['metadata']['name']=='pd-pingdirectory': self.assertEqual(doc['spec']['replicas'],3)

    def test_secret_or_routing_export_rejected(self):
        for kind,name in (('Secret','runtime'),('ConfigMap','pd-routing'),('Service','pf-active-engine')):
            with self.assertRaises(ValueError): gitops.protect({'kind':kind,'metadata':{'name':name}})

    def test_origin_requires_access_and_tls(self):
        with self.assertRaises(ValueError): gitops.argo_values(self.config,public=True)
        config={**self.config,'access_policy_configured':True,
            'origin_certificate_arn':'arn:aws:acm:ap-southeast-2:123456789012:certificate/example',
            'cloudflare_ipv4_cidrs':['173.245.48.0/20']}
        values=gitops.argo_values(config,public=True)
        self.assertEqual(values['server']['ingress']['hostname'],'argocd.eks.darkedges.com')
        self.assertEqual(values['server']['ingress']['annotations']['alb.ingress.kubernetes.io/backend-protocol'],'HTTPS')
        self.assertFalse(values['configs']['params']['server.insecure'])
        self.assertTrue(values['configs']['params']['server.repo.server.strict.tls'])

    def test_project_cannot_manage_cluster_resources_or_secrets(self):
        spec=gitops.app_project(self.config)['spec']
        self.assertEqual(spec['clusterResourceWhitelist'],[])
        self.assertNotIn({'group':'','kind':'Secret'},spec['namespaceResourceWhitelist'])
        self.assertNotIn('argocd',{x['namespace'] for x in spec['destinations']})

    def test_chart_is_pinned_and_server_remains_tls(self):
        self.assertEqual(gitops.chart().name,'argo-cd-10.4.1.tgz')
        docs=[x for x in yaml.safe_load_all((project.BUILD/'argocd.manifests.yaml').read_text()) if x]
        params=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='argocd-cmd-params-cm')['data']
        self.assertEqual(params['server.insecure'],'false')
        self.assertEqual(params['server.repo.server.strict.tls'],'true')
        self.assertFalse(any(x['kind']=='Ingress' for x in docs))
