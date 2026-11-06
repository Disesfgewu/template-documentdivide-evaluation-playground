"""Evidence must report fixture input failures, including before named cases."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.parser import test_fixture_acceptance as acceptance


class FixtureEvidenceFailureTests(unittest.TestCase):
    def test_bad_input_hash_cannot_emit_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LAB_PDF_FIXTURE_REPORT_DIR': directory}):
                with patch.object(acceptance.rc, 'item_bytes', return_value=b'wrong fixture bytes'):
                    suite = unittest.TestSuite([
                        acceptance.FixtureAcceptance('test_f01_hash_counts_and_physical_indices')])
                    result = unittest.TestResult()
                    suite.run(result)
            self.assertEqual(len(result.failures), 1)
            report = json.loads((Path(directory) / 'evidence.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['cases'], [])

    def test_registry_setup_error_replaces_stale_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / 'evidence.json'
            report_path.write_text(json.dumps({'status': 'passed', 'commit': 'stale'}))
            with patch.dict(os.environ, {'LAB_PDF_FIXTURE_REPORT_DIR': directory}):
                with patch.object(acceptance.rc, 'load', side_effect=ValueError('invalid registry')):
                    suite = unittest.TestSuite([
                        acceptance.FixtureAcceptance('test_f01_hash_counts_and_physical_indices')])
                    result = unittest.TestResult()
                    suite.run(result)
            self.assertEqual(len(result.errors), 1)
            report = json.loads(report_path.read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertNotEqual(report['commit'], 'stale')
            self.assertEqual(report['cases'], [])


if __name__ == '__main__':
    unittest.main()
