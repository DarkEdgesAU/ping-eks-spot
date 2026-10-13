import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import promotion_evidence as e


class EvidenceGenerationTests(unittest.TestCase):
    def generate(self, attest):
        c = e.project.config()
        with patch.object(e.project, 'check_context'), patch.object(e.project, 'ready_pods') as ready:
            result = e.generate('blue', 'operator', 'Verified report paths', attest)
            ready.assert_called_once_with('identity-blue', 'pingdirectory', c['image_tag'])
            return result, c

    def test_draft_never_attests_verification(self):
        result, c = self.generate(False)
        self.assertTrue(all(result[f] is False for f in e.FLAGS))
        with self.assertRaises(ValueError): e.project.validate_evidence(result, c, 'blue')

    def test_explicit_attestation_has_valid_metadata(self):
        result, c = self.generate(True)
        e.project.validate_evidence(result, c, 'blue')
        self.assertEqual(result['base_dn'], c['base_dn'])
