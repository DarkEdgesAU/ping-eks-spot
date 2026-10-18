from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import management_bootstrap as bootstrap


class BootstrapTests(unittest.TestCase):
    def test_denied_request_identifies_stage(self):
        vault = Mock()
        vault.call.side_effect = ValueError('Secret service request failed (HTTP 403)')
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'GET sys/mounts failed.*HTTP 403'):
            bootstrap.configure(vault)

    def test_arbitrary_exception_content_is_never_reported(self):
        vault = Mock(); vault.call.side_effect = ValueError('private-token-value')
        with self.assertRaises(bootstrap.BootstrapError) as caught:
            bootstrap.configure(vault)
        self.assertNotIn('private-token-value', str(caught.exception))

    def test_incompatible_mount_is_not_overwritten(self):
        vault = Mock(); vault.call.return_value = {'data': {'ping-eks/': {'type': 'transit'}}}
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'not KV v2'):
            bootstrap.configure(vault)
        self.assertEqual(vault.call.call_count, 1)
