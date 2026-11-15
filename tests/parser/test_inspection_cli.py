"""Real subprocess CLI checks using registry-generated, hash-pinned PDFs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixtures import registry_check as registry
from lab_parser.inspection_cli import _read_regular, inspect

ROOT = Path(__file__).resolve().parents[2]


class InspectionCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = {item['id']: item for entry in registry.load()['fixtures'] for item in entry['items']}

    def data(self, fid):
        item = self.fixtures[fid]
        data = registry.item_bytes(item)
        self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
        return data

    def run_cli(self, args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('LAB_PARSER_')}
        env['PYTHONPATH'] = str(ROOT / 'services/parser')
        return subprocess.run([sys.executable, '-m', 'lab_parser', 'inspect', *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)

    def inspect_bytes(self, data, *flags):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'credential-SECRET.pdf'
            path.write_bytes(data)
            result = self.run_cli([str(path), *flags])
        self.assertEqual(result.stderr, '')
        self.assertNotIn('SECRET', result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report['format'], 'lab-pdf-inspection-1')
        return result.returncode, report

    def test_f01_real_counts_hash_and_no_service_config(self):
        for fid, count in [('F01.pages_1', 1), ('F01.pages_21', 21), ('F01.pages_600', 600)]:
            with self.subTest(fid=fid):
                data = self.data(fid)
                code, report = self.inspect_bytes(data)
                self.assertEqual(code, 0)
                self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(report['size_bytes'], len(data))
                self.assertTrue(report['complete_read'])
                self.assertEqual(report['page_count'], count)
                self.assertEqual([p['page_index'] for p in report['pages']], list(range(count)))

    def test_f02_fixed_geometry(self):
        entry = next(e for e in registry.load()['fixtures'] if e['id'] == 'F02')
        item = entry['items'][0]
        expected = json.loads((registry.ROOT / entry['expected']).read_text())['items'][item['id']]
        code, report = self.inspect_bytes(self.data(item['id']))
        self.assertEqual(code, 0)
        for actual, golden in zip(report['pages'], expected['pages']):
            self.assertEqual(actual['page_index'], golden['page_index'])
            self.assertEqual(actual['rotation'], golden['rotate'])
            self.assertEqual(actual['cropbox'], golden['cropbox'])
            self.assertEqual(actual['mediabox'], golden['mediabox'])
            self.assertEqual(actual['page_label'], golden['page_label'])
        self.assertEqual(len(report['pages']), len(expected['pages']))

    def test_f05_fixed_outcomes_no_diagnostic_leak(self):
        cases = [('F05.empty', 'empty_file'), ('F05.fake_text', 'not_a_pdf'),
                 ('F05.truncated', 'corrupt_pdf'), ('F05.encrypted', 'encrypted_pdf'),
                 ('F05.bad_xref', 'xref_repaired'), ('F05.many_pages', 'page_limit_exceeded')]
        for fid, reason in cases:
            with self.subTest(fid=fid):
                code, report = self.inspect_bytes(self.data(fid))
                self.assertEqual(code, 1)
                self.assertEqual(report['reason'], reason)
                self.assertEqual(report['pages'], [])
                self.assertNotIn('warnings', report)
        code, report = self.inspect_bytes(self.data('F05.injection'))
        self.assertEqual(code, 0)
        self.assertEqual(report['outcome'], 'accept')
        self.assertNotIn('IGNORE ALL', json.dumps(report))

    def test_byte_and_page_boundaries(self):
        data = self.data('F01.pages_21')
        code, report = self.inspect_bytes(data, '--max-bytes', str(len(data)), '--max-pages', '21')
        self.assertEqual(code, 0)
        code, report = self.inspect_bytes(data, '--max-bytes', str(len(data) - 1))
        self.assertEqual(code, 1)
        self.assertEqual(report['bytes_read'], len(data))
        self.assertFalse(report['complete_read'])
        self.assertIsNone(report['sha256'])
        self.assertIsNone(report['size_bytes'])
        self.assertIsNone(report['page_count'])
        code, report = self.inspect_bytes(data, '--max-pages', '20')
        self.assertEqual(code, 1)
        self.assertEqual(report['reason'], 'page_limit_exceeded')
        self.assertEqual(report['page_count'], 21)
        self.assertTrue(report['complete_read'])
        self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())

    def test_bounded_reader_and_overlimit_never_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.pdf'
            path.write_bytes(b'x' * 1000)
            self.assertEqual(len(_read_regular(str(path), 9)), 10)
            with patch('lab_parser.inspection_cli.inspect_pdf', side_effect=AssertionError('must not parse')):
                with patch('builtins.print') as printed:
                    self.assertEqual(inspect([str(path), '--max-bytes', '9']), 1)
                report = json.loads(printed.call_args.args[0])
                self.assertEqual(report['reason'], 'file_too_large')

    def test_missing_directory_fifo_and_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = str(Path(tmp) / 'credential-SECRET')
            fifo = Path(tmp) / 'fifo'
            os.mkfifo(fifo)
            for args, reason in [([secret], 'input_unavailable'), ([tmp], 'not_regular_file'),
                                 ([str(fifo)], 'not_regular_file'), ([], 'invalid_arguments'),
                                 ([secret, '--max-bytes', '0'], 'invalid_arguments'),
                                 ([secret, '--max-pages', '-1'], 'invalid_arguments'),
                                 ([secret, '--secret-token'], 'invalid_arguments')]:
                with self.subTest(args=args):
                    result = self.run_cli(args)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr, '')
                    self.assertNotIn('SECRET', result.stdout)
                    self.assertEqual(json.loads(result.stdout)['reason'], reason)

    def test_symlink_to_regular_file_is_read_from_checked_descriptor(self):
        data = self.data('F01.pages_1')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'target.pdf'; target.write_bytes(data)
            link = Path(tmp) / 'link.pdf'; link.symlink_to(target)
            result = self.run_cli([str(link)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout)['sha256'], hashlib.sha256(data).hexdigest())

    def test_geometry_failure_is_review_not_accept(self):
        data = self.data('F01.pages_1')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.pdf'; path.write_bytes(data)
            with patch('lab_parser.inspection_cli.extract_pages', side_effect=ValueError('SECRET')):
                with patch('builtins.print') as printed:
                    self.assertEqual(inspect([str(path)]), 1)
                report = json.loads(printed.call_args.args[0])
                self.assertEqual(report['outcome'], 'flag')
                self.assertEqual(report['reason'], 'invalid_page_geometry')
                self.assertEqual(report['pages'], [])
                self.assertNotIn('SECRET', str(report))
