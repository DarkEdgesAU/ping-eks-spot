import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import project


class RuntimeSecretTests(unittest.TestCase):
    def test_restart_has_base_entry_variables(self):
        for base_dn, attribute, value, object_class in (
                ('dc=example,dc=com', 'dc', 'example', 'domain'),
                ('o=Example', 'o', 'Example', 'organization')):
            config = {**project.config(), 'base_dn': base_dn}
            env = project.common_values(config)['global']['envs']
            self.assertEqual(env['BASE_ATTRIBUTE'], attribute)
            self.assertEqual(env['BASE_VALUE'], value)
            self.assertEqual(env['BASE_CLASS'], object_class)

    def test_repair_folded_paths_preserve_identity_and_repeat_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root/'source', root/'destination'
            source.mkdir()
            for name in ('keystore.p12', 'truststore.p12'):
                (source/name).write_bytes(b'fixture-only')
            config = root/'config.ldif'
            config.write_text('dn: cn=PKCS12,cn=Key Manager Providers,cn=config\n'
                'ds-cfg-key-store-file: /run/ping-secrets/..2026_10_12_22_01_11.123/\n keystore.p12\n'
                'ds-cfg-trust-store-file: /run/ping-secrets/..2026_10_12_22_01_11.123/truststore.p12\n'
                'ds-cfg-key-store-pin-file: /run/ping-secrets/..2026_10_12_22_01_11.123/keystore.pin\n'
                'ds-cfg-server-id: keep-original-server-identity\n')
            original = config.read_bytes()
            env = {**os.environ, 'SECRET_SOURCE': source.as_posix(),
                   'SECRET_DEST': destination.as_posix(), 'SERVER_CONFIG': config.as_posix()}
            command = [project.profile_shell(), (project.ROOT/'scripts/prepare_runtime.sh').as_posix()]
            subprocess.run(command, env=env, check=True, capture_output=True)
            result = config.read_text()
            self.assertNotIn('..2026', result)
            self.assertIn('ds-cfg-key-store-file: /run/ping-secrets/keystore.p12', result)
            self.assertIn('ds-cfg-key-store-pin-file: /run/ping-secrets/keystore.pin', result)
            self.assertIn('keep-original-server-identity', result)
            backups = list(root.glob('config.ldif.before-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(config.read_text(), result)
            self.assertEqual(len(list(root.glob('config.ldif.before-*'))), 1)
            self.assertEqual((destination/'keystore.p12').read_bytes(), b'fixture-only')

    def test_application_uses_copy_not_projected_secret(self):
        for target in ('blue', 'green', 'proxy'):
            v = project.values(project.config(), target)
            self.assertEqual(v['volumes']['runtime-secret-files']['emptyDir']['medium'], 'Memory')
            self.assertFalse(any(m['name'] == 'runtime-secrets' for m in v['global']['volumeMounts']))
            init = v['initContainers']['prepare-runtime']
            self.assertEqual(any(m['name'] == 'out-dir' for m in init['volumeMounts']), target != 'proxy')
