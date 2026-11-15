"""Tests for PDF import validation (SPEC R01, 16.6, T01-T04) using F05 fixtures."""

from __future__ import annotations

import hashlib
import json
import unittest

from fixtures import registry_check as rc
from lab_parser.importer import ImportResult, Limits, inspect_pdf

OUTCOME_MAP: dict[str, str] = {
    "reject": "reject",
    "flag": "flag",
    "accept_as_data": "accept",
    "reject_if_over_limit": "reject",
}


def _get_f05():
    """Retrieve F05 entry, item dictionary, and expected items mapping."""
    doc = rc.load()
    entry = next(e for e in doc["fixtures"] if e["id"] == "F05")
    items = {it["id"]: it for it in entry["items"]}
    if "expected" in entry:
        expected_items = rc._expected_items(entry["expected"])
    else:
        expected_path = rc.ROOT / "fixtures" / "F05" / "expected.json"
        expected_items = json.loads(expected_path.read_text(encoding="utf-8"))["items"]
    return entry, items, expected_items


class TestPdfImporterF05(unittest.TestCase):
    """F05 fixture acceptance tests for PDF import inspection."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.entry, cls.items, cls.expected_items = _get_f05()

    def test_all_f05_items_match_expected(self) -> None:
        """Verify each registered F05 item matches the expected outcome and reason."""
        for item_id, exp_item in self.expected_items.items():
            self.assertIn(item_id, self.items, f"Item {item_id} not registered in F05")
            data = rc.item_bytes(self.items[item_id])
            result = inspect_pdf(data)

            expected_outcome = OUTCOME_MAP[exp_item["outcome"]]
            self.assertEqual(
                result.outcome,
                expected_outcome,
                f"Outcome mismatch for {item_id}: expected {expected_outcome}, got {result.outcome}",
            )
            self.assertEqual(
                result.reason,
                exp_item["reason"],
                f"Reason mismatch for {item_id}: expected {exp_item['reason']}, got {result.reason}",
            )
            self.assertEqual(
                result.sha256,
                hashlib.sha256(data).hexdigest(),
                f"SHA256 mismatch for {item_id}",
            )
            self.assertEqual(
                result.size_bytes,
                len(data),
                f"Size mismatch for {item_id}",
            )

            # Check page count if defined in expected.json
            if "page_count" in exp_item:
                self.assertEqual(
                    result.page_count,
                    exp_item["page_count"],
                    f"Page count mismatch for {item_id}: expected {exp_item['page_count']}, got {result.page_count}",
                )

    def test_bad_xref_flagged_with_warning(self) -> None:
        """Verify F05.bad_xref outcome is 'flag', reason 'xref_repaired', and warnings collected."""
        data = rc.item_bytes(self.items["F05.bad_xref"])
        result = inspect_pdf(data)

        self.assertEqual(result.outcome, "flag")
        self.assertEqual(result.reason, "xref_repaired")
        self.assertEqual(result.page_count, 2)
        self.assertGreater(len(result.warnings), 0)
        self.assertTrue(
            any("repaired" in w.lower() for w in result.warnings),
            f"Expected warning containing 'repaired', got: {result.warnings}",
        )

    def test_encrypted_rejected_without_password_guess(self) -> None:
        """Verify F05.encrypted is rejected with reason 'encrypted_pdf' and page_count is None."""
        data = rc.item_bytes(self.items["F05.encrypted"])
        result = inspect_pdf(data)

        self.assertEqual(result.outcome, "reject")
        self.assertEqual(result.reason, "encrypted_pdf")
        self.assertIsNone(result.page_count)

    def test_injection_accepted_as_data_page_count_one(self) -> None:
        """Verify F05.injection is accepted as plain data and page_count is 1."""
        data = rc.item_bytes(self.items["F05.injection"])
        result = inspect_pdf(data)

        self.assertEqual(result.outcome, "accept")
        self.assertIsNone(result.reason)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.size_bytes, len(data))

    def test_max_bytes_small_returns_file_too_large(self) -> None:
        """Verify setting small max_bytes rejects with 'file_too_large' without parsing."""
        data = rc.item_bytes(self.items["F05.injection"])
        self.assertGreater(len(data), 10)

        result = inspect_pdf(data, limits=Limits(max_bytes=10))
        self.assertEqual(result.outcome, "reject")
        self.assertEqual(result.reason, "file_too_large")
        self.assertIsNone(result.page_count)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.size_bytes, len(data))
        self.assertEqual(result.warnings, ())

    def test_many_pages_accept_when_limit_3000(self) -> None:
        """Verify F05.many_pages (2001 pages) rejects under default limit (2000) and accepts under limit 3000."""
        data = rc.item_bytes(self.items["F05.many_pages"])

        # Default limits (max_pages=2000) -> reject page_limit_exceeded with actual page count
        res_default = inspect_pdf(data, limits=Limits())
        self.assertEqual(res_default.outcome, "reject")
        self.assertEqual(res_default.reason, "page_limit_exceeded")
        self.assertEqual(res_default.page_count, 2001)

        # Limits(max_pages=3000) -> accept with page count 2001
        res_3000 = inspect_pdf(data, limits=Limits(max_pages=3000))
        self.assertEqual(res_3000.outcome, "accept")
        self.assertIsNone(res_3000.reason)
        self.assertEqual(res_3000.page_count, 2001)
        self.assertEqual(res_3000.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(res_3000.size_bytes, len(data))

    def test_sha256_and_size_correctness(self) -> None:
        """Verify sha256 and size_bytes always match raw bytes for various files."""
        for item_id in ("F05.empty", "F05.fake_text", "F05.truncated", "F05.injection"):
            data = rc.item_bytes(self.items[item_id])
            result = inspect_pdf(data)
            self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
            self.assertEqual(result.size_bytes, len(data))


class TestPdfImporterHeaderAndEdgeCases(unittest.TestCase):
    """Detailed unit tests for header magic, whitespace, and empty file boundaries."""

    def test_empty_bytes_rejected(self) -> None:
        result = inspect_pdf(b"")
        self.assertEqual(result.outcome, "reject")
        self.assertEqual(result.reason, "empty_file")
        self.assertEqual(result.size_bytes, 0)
        self.assertEqual(
            result.sha256,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertIsNone(result.page_count)
        self.assertEqual(result.warnings, ())

    def test_leading_whitespace_before_pdf_allowed(self) -> None:
        entry, items, _ = _get_f05()
        valid_pdf = rc.item_bytes(items["F05.injection"])

        # Add ASCII whitespace before %PDF-
        spaced_pdf = b"  \r\n\t  " + valid_pdf
        result = inspect_pdf(spaced_pdf)
        # The header check tolerates leading whitespace, but the extra bytes
        # shift every xref offset, so strict parsing must not silently accept
        # the file: it is either rejected as corrupt or flagged as repaired.
        self.assertNotEqual(result.reason, "not_a_pdf")
        self.assertIn((result.outcome, result.reason), {("reject", "corrupt_pdf"), ("flag", "xref_repaired")})
        self.assertEqual(result.sha256, hashlib.sha256(spaced_pdf).hexdigest())
        self.assertEqual(result.size_bytes, len(spaced_pdf))

    def test_non_whitespace_before_pdf_rejected_as_not_a_pdf(self) -> None:
        entry, items, _ = _get_f05()
        valid_pdf = rc.item_bytes(items["F05.injection"])

        # Non-whitespace text before %PDF- must be rejected as not_a_pdf
        prefixed_pdf = b"INVALID_PREFIX " + valid_pdf
        result = inspect_pdf(prefixed_pdf)
        self.assertEqual(result.outcome, "reject")
        self.assertEqual(result.reason, "not_a_pdf")
        self.assertIsNone(result.page_count)

    def test_whitespace_exceeding_1024_bytes_rejected_as_not_a_pdf(self) -> None:
        entry, items, _ = _get_f05()
        valid_pdf = rc.item_bytes(items["F05.injection"])

        # 1024 bytes of spaces before %PDF-
        too_much_ws = b" " * 1024 + valid_pdf
        result = inspect_pdf(too_much_ws)
        self.assertEqual(result.outcome, "reject")
        self.assertEqual(result.reason, "not_a_pdf")
        self.assertIsNone(result.page_count)


class TestInspectionIsolation(unittest.TestCase):
    def test_invalid_limits_fail_before_inspection(self):
        for field in ('max_bytes', 'max_pages'):
            for value in (True, 0, -1, 1.5, '1'):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        Limits(**{field: value})

    def test_other_thread_repair_warning_does_not_flag_valid_pdf(self):
        import logging
        import threading
        from unittest.mock import patch
        from pypdf import PdfReader

        _, items, _ = _get_f05()
        data = rc.item_bytes(items['F05.injection'])
        logger = logging.getLogger('pypdf')
        before = (logger.level, tuple(logger.handlers))

        def read(*args, **kwargs):
            thread = threading.Thread(target=lambda: logger.warning('unrelated xref repaired'))
            thread.start()
            thread.join()
            return PdfReader(*args, **kwargs)

        with patch('lab_parser.importer.PdfReader', side_effect=read):
            result = inspect_pdf(data)
        self.assertEqual(result.outcome, 'accept')
        self.assertEqual(result.warnings, ())
        self.assertEqual((logger.level, tuple(logger.handlers)), before)


if __name__ == "__main__":
    unittest.main()
