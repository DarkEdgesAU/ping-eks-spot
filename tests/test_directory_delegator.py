"""Regression guards for Directory's two-phase Delegator bootstrap."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import project
import platform_services
import federate


class DirectoryDelegatorTests(unittest.TestCase):
    def test_profile_defines_identity_plugins_without_pf_startup_dependency(self):
        common = (project.ROOT/'profiles/pingdirectory/pd.profile/dsconfig/50-delegated-admin.dsconfig').read_text()
        self.assertIn('--plugin-name pf-connected-identity ', common)
        self.assertIn('--plugin-name pf-connected-identities ', common)
        self.assertIn('ds-pwp-account-disabled', common)
        self.assertNotIn('create-access-token-validator', common)
        self.assertNotIn('http-servlet-extension:Delegated Admin', common)
        self.assertNotIn('Blind Trust', common)
        self.assertNotIn('cors-allowed-origins: *', common)
        signing = (platform_services.SOURCE/'delegator.dsconfig').read_text()
        self.assertIn('allowed-signing-algorithm:RS256', signing)
        self.assertIn('http-servlet-extension:Delegated Admin', signing)

    def test_oauth_seed_preserves_clients_and_substitutes_credentials(self):
        seed = (project.ROOT/'profiles/pingdirectory/pd.profile/ldif/userRoot/30-OAuthClients.ldif').read_text()
        import re
        ids = re.findall(r'^dn: pf-oauth-client-id=([^,]+),', seed, re.M)
        self.assertEqual(set(ids), {'pingaccessAc','ro_client','cc_secret_client','ac_client',
            'rs_client','saml_client','cc_jwt_client','df_client','ac_oic_client','jwt_client',
            'im_client','im_oic_client','dadmin','pingdirectory'})
        self.assertEqual(len(ids), 14)
        import oauth_client_secrets
        mapping = oauth_client_secrets.mapping()
        hook = (project.ROOT/'profiles/pingdirectory/hooks/03-build-run-plan.sh.pre').read_text()
        for item in mapping['hashes']+mapping['jwks']+mapping['reversible']:
            self.assertIn('${'+item['variable']+'}', seed)
            self.assertIn('test -s /run/ping-secrets/'+item['file'], hook)
            self.assertIn(item['variable']+'=%s', hook)
        for line in seed.splitlines():
            if line.startswith(('pf-oauth-client-hashed-secret:', 'pf-oauth-client-jwks:')) or ':REVERSIBLE_SECRET:' in line:
                self.assertIn('${OAUTH_CLIENT_', line)
        dadmin = next(block for block in re.split(r'\n\s*\n', seed) if 'dn: pf-oauth-client-id=dadmin,' in block)
        self.assertIn('pf-oauth-client-authn-type: NONE', dadmin)
        self.assertIn('7:ENABLED:false', dadmin)
        self.assertIn('pf-oauth-client-grant-type: authorization_code', dadmin)
        self.assertNotIn('/delegator/*', dadmin)
        self.assertNotIn('grant-type: implicit', dadmin)

    def test_directory_and_pf_use_the_same_delegator_origin(self):
        c = platform_services.config()
        directory = project.common_values(project.config())['global']['envs']
        pf = federate.values(federate.config(), 'blue')['global']['envs']
        self.assertEqual(directory['PD_DELEGATOR_PUBLIC_HOSTNAME'], c['delegator_hostname'])
        self.assertEqual(pf['PD_DELEGATOR_PUBLIC_HOSTNAME'], c['delegator_hostname'])
        self.assertEqual(pf['PD_DELEGATOR_PUBLIC_PORT_HTTPS'], '443')


if __name__ == '__main__': unittest.main()
