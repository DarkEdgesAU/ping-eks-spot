import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from lifecycle import logging_secrets_from_files


class RestoreLoggingTests(unittest.TestCase):
    def workload(self, name='log-shipper'):
        return {'kind': 'StatefulSet', 'metadata': {'namespace': 'identity-blue'},
                'spec': {'template': {'spec': {'volumes': [{'secret': {'secretName': name}}]}}}}

    def test_exact_original_bytes_and_deduplication(self):
        files = {'.secrets/logging/shipper-password': base64.b64encode(b'original\r\n').decode(),
                 '.secrets/logging/ca.crt': base64.b64encode(b'original-ca').decode()}
        docs = logging_secrets_from_files(files, [self.workload(), self.workload()])
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['data']['password'], files['.secrets/logging/shipper-password'])
        self.assertEqual(docs[0]['data']['ca.crt'], files['.secrets/logging/ca.crt'])

    def test_disabled_logging_requires_no_private_files(self):
        self.assertEqual(logging_secrets_from_files({}, [self.workload('other-secret')]), [])

    def test_missing_or_invalid_saved_material_fails_closed(self):
        for files in ({}, {'.secrets/logging/shipper-password': '!'}) :
            with self.assertRaises(ValueError):
                logging_secrets_from_files(files, [self.workload()])

    def test_environment_secret_reference_is_detected(self):
        doc = self.workload()
        pod = doc['spec']['template']['spec']
        pod['volumes'] = []
        pod['containers'] = [{'env': [{'valueFrom': {'secretKeyRef': {'name': 'log-shipper'}}}]}]
        files = {'.secrets/logging/'+name: base64.b64encode(b'original').decode()
                 for name in ('shipper-password', 'ca.crt')}
        self.assertEqual(len(logging_secrets_from_files(files, [doc])), 1)
