from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import observability
from lifecycle import logging_secrets_from_files


class DashboardsBootstrapTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for bootstrap API tests')
    def test_api_preserves_existing_objects_and_is_repeatable(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('dashboards-bootstrap.test.js'))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_job_is_postsync_bounded_and_has_no_service_account_or_inline_secrets(self):
        job = next(d for d in observability.manifests() if d['kind'] == 'Job')
        self.assertEqual(job['metadata']['annotations']['argocd.argoproj.io/hook'], 'PostSync')
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 600)
        pod = job['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(all(v['readOnly'] for v in pod['containers'][0]['volumeMounts']))
        self.assertNotIn('env', pod['containers'][0])
        self.assertEqual(pod['volumes'][2]['secret']['items'], [{'key': 'ca.crt', 'path': 'ca.crt'}])

    def test_restore_detects_job_admin_secret_from_pinned_files(self):
        job = next(d for d in observability.manifests() if d['kind'] == 'Job')
        import base64
        files = {'.secrets/logging/'+n: base64.b64encode(b'original').decode()
                 for n in ('admin-password', 'opensearch.crt', 'opensearch.key', 'dashboards.crt', 'dashboards.key', 'ca.crt')}
        secrets = logging_secrets_from_files(files, [job])
        self.assertEqual({s['metadata']['name'] for s in secrets}, {'logging-bootstrap', 'logging-tls'})
