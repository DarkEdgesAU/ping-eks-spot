import csv
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cutover_test', Path(__file__).resolve().parents[1] / 'scripts/cutover_test.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CutoverReportTests(unittest.TestCase):
    def report(self, rows, markers=None, reconciliation=None, completed=True):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            if completed: (run / 'load-completed.json').write_text('{}')
            with (run / 'load.jtl').open('w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=['timeStamp', 'elapsed', 'success'])
                w.writeheader()
                w.writerows(rows)
            if markers is not None: (run / 'markers.json').write_text(json.dumps(markers))
            if reconciliation is not None: (run / 'reconciliation.json').write_text(json.dumps(reconciliation))
            with contextlib.redirect_stdout(io.StringIO()):
                passed = c.report(run, {'max_errors': 0, 'max_p95_ms': 100})
            return passed, json.loads((run / 'summary.json').read_text())

    def rows(self):
        return [{'timeStamp': t, 'elapsed': 10, 'success': 'true'} for t in (100, 200, 300)]

    def test_missing_evidence_never_passes(self):
        self.assertFalse(self.report(self.rows())[0])

    def test_complete_observed_run_passes(self):
        self.assertTrue(self.report(self.rows(), {'promotion-start': 190, 'promotion-end': 250}, {'passed': True, 'uncertain': 0})[0])

    def test_uncertain_write_fails(self):
        self.assertFalse(self.report(self.rows(), {'promotion-start': 190, 'promotion-end': 250}, {'passed': True, 'uncertain': 1})[0])

    def test_interrupted_load_fails(self):
        self.assertFalse(self.report(self.rows(), {'promotion-start': 190, 'promotion-end': 250}, {'passed': True, 'uncertain': 0}, completed=False)[0])

    def test_request_crossing_boundary_counts_during(self):
        rows = self.rows()
        rows[0]['elapsed'] = 95
        _, report = self.report(rows, {'promotion-start': 190, 'promotion-end': 250})
        self.assertEqual(report['stages']['during']['requests'], 2)

    def test_failure_and_latency_thresholds(self):
        for field, value in [('success', 'false'), ('elapsed', 101)]:
            rows = self.rows()
            rows[1][field] = value
            self.assertFalse(self.report(rows, {'promotion-start': 190, 'promotion-end': 250}, {'passed': True, 'uncertain': 0})[0])

    def test_empty_phase_fails(self):
        self.assertFalse(self.report(self.rows(), {'promotion-start': 400, 'promotion-end': 500}, {'passed': True, 'uncertain': 0})[0])

    def test_plan_rate_is_shared_and_per_minute(self):
        tree = c.plan('load', {'clients': 20, 'rate': 50, 'duration': 600})
        self.assertEqual(tree.find('.//ConstantThroughputTimer/doubleProp/value').text, '3000.0')
        self.assertEqual(tree.find('.//ConstantThroughputTimer/intProp').text, '4')

    def test_plain_ldap_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            config = json.loads((c.ROOT / 'loadtest/config.example.json').read_text())
            config['proxy_url'] = 'ldap://localhost:389'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError): c.load_config(path)
