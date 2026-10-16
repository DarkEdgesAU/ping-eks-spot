import sys
from pathlib import Path
import unittest
import re
from unittest.mock import patch
from contextlib import contextmanager
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import access_config


class IdentityProxyTests(unittest.TestCase):
    def test_rewrite_preserves_query_and_only_matches_prefix(self):
        rule = access_config.identity_rule('/identity')['configuration']
        match = re.match(rule['mapFrom'], '/identity/as/authorization.oauth2?client_id=example')
        self.assertEqual('/'+match.group(1), '/as/authorization.oauth2?client_id=example')
        self.assertIsNone(re.match(rule['mapFrom'], '/identity-other/as/token.oauth2'))

    def test_plan_does_not_mutate_apis(self):
        calls = []
        @contextmanager
        def admin(colour, product='pa'):
            def request(path, data=None, method='GET'):
                calls.append((method, data))
                return {'federationInfo': {'baseUrl': 'https://old.example'}} if product == 'pf' else {}
            yield request
        with patch.object(access_config, 'admin', admin), patch.object(access_config.access.project, 'check_context'):
            access_config.configure('blue')
        self.assertTrue(calls)
        self.assertTrue(all(method == 'GET' and data is None for method, data in calls))

    def test_colours_use_private_matching_backends_and_verified_tls(self):
        for colour in ('blue', 'green'):
            result = access_config.runtime(colour, 'access.eks.darkedges.com', '/identity', 4, 5)
            self.assertEqual(result['targets'], [f'pf-{colour}-pingfederate-engine.identity-federate-{colour}.svc.cluster.local:9031'])
            self.assertTrue(result['backChannelSecure'])
            self.assertFalse(result['skipHostnameVerification'])
            self.assertEqual(result['application']['contextRoot'], '/identity')
            self.assertEqual(result['trustedCertificateGroupId'], 5)

    def test_invalid_context_root_rejected(self):
        for root in ('identity', '/identity/', '/identity?x', '/identity#x'):
            with self.assertRaises(ValueError):
                access_config.runtime('blue', 'access.eks.darkedges.com', root, 4, 5)
