import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import lifecycle


class RestoreNamespaceTests(unittest.TestCase):
    def exercise(self, namespace):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps = root/'gitops/applications'
            apps.mkdir(parents=True)
            (apps/'project.yaml').write_text('kind: AppProject\n')
            (apps/'metrics.yaml').write_text(json.dumps({'kind': 'Application',
                'metadata': {'name': 'metrics'}, 'spec': {'destination': {'namespace': namespace}}}))
            (root/'config.json').write_text('{"initial_seed_colour":"blue"}')
            instance = object.__new__(lifecycle.Lifecycle)
            instance.c = {'verification_commands': []}
            instance.verify_ldap = Mock()
            instance.put = Mock()
            calls = []
            operation = {}

            def kube(*args, data=None):
                calls.append((args, json.loads(data) if data else None))
                if 'patch' in args:
                    operation.update(json.loads(args[-1])['operation'])
                if 'get' in args:
                    return json.dumps({'status': {'sync': {'status': 'Synced'},
                        'health': {'status': 'Healthy'}, 'operationState': {
                            'phase': 'Succeeded', 'operation': operation}}})
                return ''

            instance.kube = kube
            with patch.object(lifecycle, 'ROOT', root):
                instance.restore_workloads({'inventories': {}}, 'backup', {})
            return calls, instance

    def test_new_optional_namespace_precedes_application(self):
        calls, instance = self.exercise('monitoring')
        docs = [doc for _, doc in calls if doc]
        self.assertEqual(docs[0], {'apiVersion': 'v1', 'kind': 'Namespace',
                                  'metadata': {'name': 'monitoring'}})
        self.assertEqual(docs[1]['kind'], 'Application')
        instance.verify_ldap.assert_called_once()
        instance.put.assert_called_once()

    def test_unexpected_destination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected application destination'):
            self.exercise('unrelated-namespace')
