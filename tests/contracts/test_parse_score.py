import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from lab_contracts.parse import CAPABILITIES, CapabilityManifest, ParseManifest, ParseRequest, ParseResult
from lab_contracts.score import MetricResult, display, display_ratio

EX = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "valid"


def load(model, name, **upd):
    obj = json.loads((EX / f"{name}.json").read_text())
    obj.update(upd)
    return model.model_validate_json(json.dumps(obj))


class ParseManifestTests(unittest.TestCase):
    def manifest(self, **upd):
        return load(ParseManifest, "ParseManifest.pinned_with_fallback", **upd)

    def test_floating_or_case_variant_versions_are_not_reproducible(self):
        self.manifest()
        for v in ("latest", "Unknown", "UNKNOWN", " main ", "pkg@latest"):
            with self.subTest(v=v), self.assertRaises(ValidationError):
                self.manifest(package_or_service_version=v)

    def test_unknown_allowed_when_not_reproducible(self):
        m = self.manifest(package_or_service_version="unknown", reproducible=False)
        self.assertFalse(m.reproducible)

    def test_duplicate_languages_rejected(self):
        with self.assertRaises(ValidationError):
            self.manifest(ocr_languages=["en", "en"])


class ParseContractTests(unittest.TestCase):
    def test_capability_fields_match_spec_list(self):
        fields = set(CapabilityManifest.model_fields) - {"parser_name", "adapter_revision"}
        self.assertEqual(fields, set(CAPABILITIES))

    def test_result_must_cover_requested_pages(self):
        req = load(ParseRequest, "ParseRequest.three_pages")
        res = load(ParseResult, "ParseResult.mixed_pages")
        self.assertEqual(res.check_against_request(req), [])
        fewer = load(ParseRequest, "ParseRequest.three_pages", page_indices=[0, 1, 2, 3])
        self.assertEqual(res.check_against_request(fewer), ["requested_pages_missing"])
        other = load(ParseRequest, "ParseRequest.three_pages", page_indices=[0, 1], operation_id="op_x")
        self.assertEqual(res.check_against_request(other),
                         ["operation_id_mismatch", "unrequested_pages_reported"])


class ParseResultNegativeTests(unittest.TestCase):
    def test_duplicate_reported_page_rejected(self):
        obj = json.loads((EX / "ParseResult.mixed_pages.json").read_text())
        obj["pages"].append(dict(obj["pages"][0]))
        with self.assertRaises(ValidationError) as cm:
            ParseResult.model_validate_json(json.dumps(obj))
        self.assertIn("page_index", str(cm.exception))

    def test_missing_extra_and_wrong_binding_reported_together(self):
        req = load(ParseRequest, "ParseRequest.three_pages", page_indices=[0, 9], operation_id="op_other")
        res = load(ParseResult, "ParseResult.mixed_pages")
        self.assertEqual(res.check_against_request(req),
                         ["operation_id_mismatch", "unrequested_pages_reported", "requested_pages_missing"])


def metric(**kw):
    base = dict(question_id="q", metric="correctness", status="scored", reason_codes=[],
                grader_revision="g@1", support_refs=[], rounding="lab-round-1")
    base.update(kw)
    return MetricResult.model_validate_json(json.dumps(base))


class ScoreContractTests(unittest.TestCase):
    def test_t37_recall_half(self):
        m = metric(metric="recall_at_k", numerator=1.0, denominator=2.0, raw_value=50.0, display_value="50.00")
        self.assertEqual(m.display_value, "50.00")

    def test_t38_weighted_correctness(self):
        # weights 1,1 and fact scores 1, 0.5 -> 1.5 / 2 -> 75.
        metric(numerator=1.0 * 1 + 1.0 * 0.5, denominator=2.0, raw_value=75.0, display_value="75.00")

    def test_full_precision_kept_display_rounded(self):
        m = metric(numerator=1.0, denominator=3.0, raw_value=100.0 / 3, display_value="33.33")
        self.assertNotEqual(m.raw_value, 33.33)
        self.assertEqual(display(200.0 / 3), "66.67")
        # Half-up on the shortest decimal repr, not on the binary value
        # (12.345 is stored as 12.3449999...).
        self.assertEqual(display(12.345), "12.35")
        self.assertEqual(display(0.125), "0.13")
        self.assertEqual(display(100.0), "100.00")

    def test_display_from_decimal_ratio_not_binary_float(self):
        # 100 * 0.00435 is 0.43499999999999994 in binary; the display uses the
        # decimal ratio 0.435 and shows 0.44.
        raw = 100.0 * 0.00435 / 1.0
        self.assertEqual(repr(raw), "0.43499999999999994")
        m = metric(numerator=0.00435, denominator=1.0, raw_value=raw, display_value="0.44")
        self.assertEqual(m.display_value, "0.44")
        with self.assertRaises(ValidationError):
            metric(numerator=0.00435, denominator=1.0, raw_value=raw, display_value="0.43")

    def test_pending_error_carry_no_ratio_na_only_zero_denominator(self):
        for status in ("pending", "error"):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                metric(status=status, raw_value=None, display_value=None, numerator=0.0,
                       denominator=10.0, reason_codes=["x"])
        with self.assertRaises(ValidationError):
            metric(status="not_applicable", raw_value=None, display_value=None, numerator=0.0,
                   denominator=3.0, reason_codes=["x"])
        metric(status="not_applicable", raw_value=None, display_value=None, numerator=0.0,
               denominator=0.0, reason_codes=["no_applicable_checks"])

    def test_large_equal_ratio_does_not_overflow(self):
        # 100.0 * 1e308 is inf in binary; the ratio is evaluated in decimal.
        m = metric(numerator=1e308, denominator=1e308, raw_value=100.0, display_value="100.00")
        self.assertEqual(m.raw_value, 100.0)
        m = metric(numerator=5e307, denominator=1e308, raw_value=50.0, display_value="50.00")
        self.assertEqual(m.display_value, "50.00")
        with self.assertRaises(ValidationError):
            metric(numerator=1e308, denominator=1e308, raw_value=99.0, display_value="100.00")

    def test_out_of_range_ratio_rejected(self):
        # numerator > denominator is never a valid percentage, even when
        # raw_value is clamped into range or the ratio would overflow.
        for num, den in ((3.0, 2.0), (1e308, 1e307), (1.0000001, 1.0)):
            for raw, disp in ((100.0, "100.00"), (min(100.0 * num / den, 100.0), "100.00")):
                with self.subTest(num=num, den=den, raw=raw), self.assertRaises(ValidationError):
                    metric(numerator=num, denominator=den, raw_value=raw, display_value=disp)

    def test_not_applicable_ratio_representations(self):
        na = dict(status="not_applicable", raw_value=None, display_value=None,
                  reason_codes=["no_applicable_checks"])
        for num, den in ((None, None), (0.0, 0.0)):
            with self.subTest(num=num, den=den):
                metric(numerator=num, denominator=den, **na)
        for num, den in ((1.0, 0.0), (1.0, None), (0.0, None), (None, 0.0), (2.0, 5.0), (0.0, 1.0)):
            with self.subTest(num=num, den=den), self.assertRaises(ValidationError):
                metric(numerator=num, denominator=den, **{**na, "reason_codes": ["none"]})

    def test_rounding_version_is_pinned(self):
        from lab_contracts.score import ROUNDING_VERSION
        self.assertEqual(ROUNDING_VERSION, "lab-round-1")
        for bad in ("lab-round-2", "", None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                metric(numerator=1.0, denominator=2.0, raw_value=50.0, display_value="50.00", rounding=bad)

    def test_ratio_ties_round_half_up(self):
        # Exact decimal ties on the ratio path: 1/800 -> 0.125 -> "0.13";
        # 1/1600 -> 0.0625 -> "0.06" (not a tie at 2 places, rounds down).
        self.assertEqual(display_ratio(1.0, 800.0), "0.13")
        self.assertEqual(display_ratio(1.0, 1600.0), "0.06")
        self.assertEqual(display_ratio(5.0, 8000.0), "0.06")   # 0.0625
        self.assertEqual(display_ratio(1.0, 8.0), "12.50")
        metric(numerator=1.0, denominator=800.0, raw_value=0.125, display_value="0.13")
        with self.assertRaises(ValidationError):
            metric(numerator=1.0, denominator=800.0, raw_value=0.125, display_value="0.12")

    def test_non_finite_numbers_rejected(self):
        # JSON has no NaN/Infinity literals; Python's json emits them, the
        # contract must still refuse them.
        for field in ("numerator", "denominator", "raw_value"):
            for bad in (float("nan"), float("inf")):
                kw = dict(numerator=1.0, denominator=2.0, raw_value=50.0, display_value="50.00")
                kw[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(ValidationError):
                    metric(**kw)

    def test_error_status_carries_no_number(self):
        with self.assertRaises(ValidationError):
            metric(status="error", raw_value=0.0, display_value="0.00", reason_codes=["grader_fault"])
        m = metric(status="error", raw_value=None, display_value=None, numerator=None,
                   denominator=None, reason_codes=["grader_fault"])
        self.assertIsNone(m.raw_value)


if __name__ == "__main__":
    unittest.main()
