import copy
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import observability as obs


class LoggingTests(unittest.TestCase):
    def test_gitops_recovers_pre_rename_configuration(self):
        import gitops
        import json
        original=json.loads((gitops.GITOPS/'config.example.json').read_text())
        for url, expected in [
            ('https://github.com/DarkEdgesAU/pingdirectory-eks-spot.git',
             'https://github.com/DarkEdgesAU/ping-eks-spot.git'),
            ('https://github.com/another/project.git','https://github.com/another/project.git')]:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                (root/'config.json').write_text(json.dumps({**original,'repo_url':url}))
                with patch.object(gitops,'GITOPS',root):
                    self.assertEqual(gitops.config()['repo_url'],expected)

    def test_gitops_config_works_in_clean_checkout(self):
        import gitops
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'config.example.json').write_text((gitops.GITOPS/'config.example.json').read_text())
            with patch.object(gitops,'GITOPS',root):
                self.assertEqual(gitops.config()['revision'],'main')

    def fixture(self, persistent):
        spec={'containers':[{'name':'pingfederate-engine','image':'example:1'}]}
        if persistent:
            spec['containers'][0]['volumeMounts']=[{'name':'out-dir','mountPath':'/opt/out'}]
        return [{'kind':'Deployment','metadata':{'name':'pf','namespace':'identity-federate-blue'},
                 'spec':{'replicas':3,'template':{'spec':spec}}}]

    def test_disabled_preserves_original_workload(self):
        docs=self.fixture(False)
        self.assertIs(obs.attach(docs,False),docs)

    def test_existing_volume_reused_and_ephemeral_volume_shared(self):
        for persistent in (False,True):
            docs=self.fixture(persistent); before=copy.deepcopy(docs)
            rendered=obs.attach(docs,True)
            self.assertEqual(docs,before)
            self.assertEqual(rendered[0]['spec']['replicas'],3)
            pod=rendered[0]['spec']['template']['spec']
            main,shipper=pod['containers']
            source=next(x for x in main['volumeMounts'] if x['mountPath']=='/opt/out')
            logs=next(x for x in shipper['volumeMounts'] if x['mountPath']=='/logs')
            self.assertEqual(source['name'],logs['name'])
            self.assertTrue(logs['readOnly'])
            self.assertFalse(any(x.get('mountPath')=='/run/ping-secrets' for x in shipper['volumeMounts']))
            self.assertEqual(shipper['env'][-1]['valueFrom']['secretKeyRef']['name'],'log-shipper')
            self.assertFalse(any(x['kind']=='Secret' for x in rendered))

    def test_manifest_security_retention_and_namespace_scope(self):
        docs=obs.manifests()
        self.assertTrue(all(x['metadata']['namespace']=='logging' for x in docs))
        self.assertFalse(any(x['kind'] in ('Secret','ClusterRole','ClusterRoleBinding','Ingress') for x in docs))
        sts=next(x for x in docs if x['kind']=='StatefulSet')
        self.assertEqual(sts['spec']['replicas'],1)
        self.assertEqual(sts['spec']['persistentVolumeClaimRetentionPolicy'],{'whenDeleted':'Retain','whenScaled':'Retain'})
        self.assertEqual(sts['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'],'pingdirectory-ebs')
        self.assertTrue(all(x['spec']['template']['spec']['automountServiceAccountToken'] is False
            for x in docs if x['kind'] in ('StatefulSet','Deployment','DaemonSet')))
        ui=next(x for x in docs if x['kind']=='Deployment')
        self.assertEqual(ui['spec']['template']['spec']['containers'][0]['readinessProbe']['httpGet']['path'],'/app/login')

    def test_output_requires_verified_tls_and_secret_password(self):
        text=(obs.SOURCE/'sidecar.conf').read_text()
        self.assertIn('tls.verify On',text)
        self.assertIn('tls.verify_hostname On',text)
        self.assertIn('HTTP_Passwd ${LOG_PASSWORD}',text)
