import base64
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import lifecycle as life


class ArgoRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test-ca')])
        now = dt.datetime.now(dt.timezone.utc)
        cls.ca = x509.CertificateBuilder().subject_name(cls.name).issuer_name(cls.name).public_key(
            cls.ca_key.public_key()).serial_number(1).not_valid_before(now-dt.timedelta(days=1)).not_valid_after(
            now+dt.timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(cls.ca_key, hashes.SHA256())
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def files(self, expired=False):
        now = dt.datetime.now(dt.timezone.utc)
        cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
            'argocd-repo-server')])).issuer_name(self.name).public_key(self.key.public_key()).serial_number(2).not_valid_before(
            now-dt.timedelta(days=2)).not_valid_after(now+dt.timedelta(days=-1 if expired else 1)).sign(self.ca_key, hashes.SHA256())
        encode = lambda data: base64.b64encode(data).decode()
        files = {'.secrets/ca.crt': encode(self.ca.public_bytes(serialization.Encoding.PEM))}
        for name in ('argocd-server-tls', 'argocd-repo-server-tls'):
            files['.secrets/argocd/'+name+'.crt'] = encode(cert.public_bytes(serialization.Encoding.PEM))
            files['.secrets/argocd/'+name+'.key'] = encode(self.key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return files

    def test_original_private_keys_and_chain_are_preserved(self):
        files = self.files()
        for secret in life.argocd_tls_from_files(files):
            name = secret['metadata']['name']
            self.assertEqual(secret['data']['tls.key'], files['.secrets/argocd/'+name+'.key'])
            self.assertEqual(base64.b64decode(secret['data']['tls.crt']),
                base64.b64decode(files['.secrets/argocd/'+name+'.crt'])+base64.b64decode(files['.secrets/ca.crt']))

    def test_missing_tls_material_fails_without_generation(self):
        files = self.files(); files.pop('.secrets/argocd/argocd-server-tls.key')
        with self.assertRaises(ValueError): life.argocd_tls_from_files(files)

    def test_mismatched_private_key_is_rejected(self):
        files = self.files()
        files['.secrets/argocd/argocd-server-tls.key'] = base64.b64encode(self.ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()
        with self.assertRaisesRegex(ValueError, 'does not match'): life.argocd_tls_from_files(files)

    def test_expired_listener_is_rejected_before_provisioning(self):
        with self.assertRaisesRegex(ValueError, 'not currently valid'): life.argocd_tls_from_files(self.files(True))

    def test_helm_owned_configmaps_follow_bootstrap_but_tls_and_routing_precede_it(self):
        events = []
        runner = life.Lifecycle.__new__(life.Lifecycle)
        runner.kube = lambda *args, data=None: events.append(json.loads(data)['metadata']['name'])
        prepared = {'00-namespaces.yaml': [], '30-retained-claims.yaml': [],
                    '20-runtime-secrets.yaml': life.argocd_tls_from_files(self.files()),
                    '40-runtime-configmaps.yaml': [
                        {'metadata': {'namespace': 'argocd', 'name': 'argocd-tls-certs-cm'}},
                        {'metadata': {'namespace': 'identity-proxy', 'name': 'pd-routing'}}]}
        def command(args): events.append(Path(args[1]).name)
        with tempfile.TemporaryDirectory() as folder, patch.object(life, 'ROOT', Path(folder)), patch.object(life, 'command', command):
            (Path(folder)/'.secrets').mkdir()
            runner.bootstrap_runtime(prepared)
        for name in ('argocd-server-tls', 'argocd-repo-server-tls', 'pd-routing'):
            self.assertLess(events.index(name), events.index('gitops.py'))
        self.assertGreater(events.index('argocd-tls-certs-cm'), events.index('gitops.py'))


if __name__ == '__main__': unittest.main()
