"""Evidence must report fixture input failures, including before named cases."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter, Transformation

from tests.parser import test_fixture_acceptance as acceptance


class FixtureEvidenceFailureTests(unittest.TestCase):
    def test_shifted_crop_cannot_pass_localization_or_emit_passed_report(self):
        original = acceptance.pdfium.PdfPage.render

        def shifted(page, *args, **kwargs):
            crop = kwargs.get('crop')
            if crop and any(crop):
                left, bottom, right, top = crop
                # Keep size/metadata and most gray fill, but move content by
                # 4 pt (>0.005 even on the largest 792 pt fixture dimension).
                kwargs['crop'] = (left + 4, bottom, right - 4, top)
            return original(page, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LAB_PDF_FIXTURE_REPORT_DIR': directory}):
                with patch.object(acceptance.pdfium.PdfPage, 'render', shifted):
                    suite = unittest.TestSuite([
                        acceptance.FixtureAcceptance('test_f02_geometry_render_and_negative_controls')])
                    result = unittest.TestResult()
                    suite.run(result)
            self.assertEqual(len(result.errors), 0)
            self.assertEqual(len(result.failures), 24)
            report = json.loads((Path(directory) / 'evidence.json').read_text())
            self.assertEqual(report['status'], 'failed')
            crops = [case for case in report['cases'] if 'full_page_pixel_match' in case['actual']]
            self.assertEqual(len(crops), 24)
            for case in crops:
                self.assertEqual(case['status'], 'failed')
                self.assertGreater(case['actual']['gray_ratio'], acceptance.GRAY_MIN_RATIO)
                self.assertGreater(case['actual']['full_page_pixel_match']['mismatched_rgb_bytes'], 0)

    def test_150_dpi_shifted_crop_cannot_pass_localization(self):
        original = acceptance.pdfium.PdfPage.render

        def shifted(page, *args, **kwargs):
            crop = kwargs.get('crop')
            if kwargs.get('scale') == 150 / 72 and crop and any(crop):
                left, bottom, right, top = crop
                kwargs['crop'] = (left + 4, bottom, right - 4, top)
            return original(page, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LAB_PDF_FIXTURE_REPORT_DIR': directory}):
                with patch.object(acceptance.pdfium.PdfPage, 'render', shifted):
                    suite = unittest.TestSuite([
                        acceptance.FixtureAcceptance('test_f02_geometry_render_and_negative_controls')])
                    result = unittest.TestResult()
                    suite.run(result)
            self.assertEqual(len(result.errors), 0)
            self.assertEqual(len(result.failures), 12)
            report = json.loads((Path(directory) / 'evidence.json').read_text())
            self.assertEqual(report['status'], 'failed')
            crops = [case for case in report['cases'] if 'full_page_pixel_match' in case['actual']]
            self.assertEqual(len(crops), 24)
            for case in crops:
                dpi = case['actual']['transform']['dpi']
                self.assertEqual(case['status'], 'failed' if dpi == 150 else 'passed')
                mismatch = case['actual']['full_page_pixel_match']['mismatched_rgb_bytes']
                if dpi == 150:
                    self.assertGreater(mismatch, 0)
                else:
                    self.assertEqual(mismatch, 0)

    def test_shared_150_dpi_content_shift_cannot_pass_full_page_oracle(self):
        original = acceptance.render_region
        shifted_data = {}

        def shifted(data, index, region=(0, 0, 1, 1), *, dpi=150):
            # Move both page and crop content in the same PDF user space.
            # Self-consistent crop/page comparisons alone cannot detect this.
            if dpi == 150 and index == 0:
                if data not in shifted_data:
                    reader = PdfReader(io.BytesIO(data))
                    writer = PdfWriter()
                    for page in reader.pages:
                        copied = writer.add_page(page)
                        copied.add_transformation(Transformation().translate(8, 0))
                    output = io.BytesIO()
                    writer.write(output)
                    shifted_data[data] = output.getvalue()
                data = shifted_data[data]
            return original(data, index, region, dpi=dpi)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LAB_PDF_FIXTURE_REPORT_DIR': directory}):
                with patch.object(acceptance, 'render_region', shifted):
                    suite = unittest.TestSuite([
                        acceptance.FixtureAcceptance('test_f02_geometry_render_and_negative_controls')])
                    result = unittest.TestResult()
                    suite.run(result)
            self.assertEqual(len(result.errors), 0)
            self.assertEqual(len(result.failures), 1)
            report = json.loads((Path(directory) / 'evidence.json').read_text())
            self.assertEqual(report['status'], 'failed')
            failed = [case['case'] for case in report['cases'] if case['status'] == 'failed']
            self.assertEqual(failed, ['page-0-full-150'])
            crops = [case for case in report['cases'] if 'full_page_pixel_match' in case['actual']]
            self.assertEqual(len(crops), 24)
            for case in crops:
                self.assertEqual(case['status'], 'passed')
                self.assertEqual(case['actual']['full_page_pixel_match']['mismatched_rgb_bytes'], 0)

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
