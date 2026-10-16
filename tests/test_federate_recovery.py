import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import federate_recovery as recovery
import federate_repair as repair


class RecoveryTests(unittest.TestCase):
    def test_individual_repair_skips_existing_objects_and_refuses_populated_identity(self):
        payload={'operations':[{'resourceType':'/dataStores','items':[{'id':'existing'},{'id':'new'}]}]}
        current={'operations':[{'resourceType':'/dataStores','items':[{'id':'existing'}]}]}
        schema={'paths':{'/dataStores':{'post':{}}}}
        self.assertEqual(repair.requests_for(payload,current,schema),[('POST','/dataStores',{'id':'new'})])
        current['operations'].append({'resourceType':'/idp/adapters','items':[{'id':'configured'}]})
        with self.assertRaises(ValueError): repair.requests_for(payload,current,schema)

    def test_individual_repair_rejects_protected_resources(self):
        for resource in recovery.PROTECTED:
            with self.assertRaises(ValueError):
                repair.requests_for({'operations':[{'resourceType':resource,'items':[{}]}]},
                                    {'operations':[]},{'paths':{}})
    def test_recovery_preserves_security_configuration_and_runtime_expressions(self):
        # Supply fake secrets for every template variable; never use local secrets in this test.
        import tempfile
        import json
        import re
        template=(recovery.pf.ROOT/'profiles/pingfederate/instance/bulk-config/data.json.subst').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); folder=root/'pf-green/vars'; folder.mkdir(parents=True)
            for name in set(re.findall(r'\$\{([^}]+)\}',template)):
                if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',name): (folder/name).write_text('test-value')
            with patch.object(recovery.pf,'SECRET',root): payload=recovery.prepare('green')
        types={op['resourceType'] for op in payload['operations']}
        self.assertFalse(types & recovery.PROTECTED)
        self.assertIn('/idp/adapters',types)
        self.assertIn('/dataStores',types)
        rendered=json.dumps(payload)
        self.assertNotIn('${USER_BASE_DN}',rendered)
        self.assertNotIn('${PRIVATE_KEYSTORE}',rendered)
        self.assertIn('${username}',rendered)
