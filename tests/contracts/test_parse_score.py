import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from lab_contracts.parse import CAPABILITIES, CapabilityManifest, ParseRequest, ParseResult
from lab_contracts.score import MetricResult, display

EX = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "valid"


def load(model, name, **upd):
    obj = json.loads((EX / f"{name}.json").read_text())
    obj.update(upd)
    return model.model_validate_json(json.dumps(obj))


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


def metric(**kw):
    base = dict(question_id="q", metric="correctness", status="scored", reason_codes=[],
                grader_revision="g@1", support_refs=[])
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

    def test_error_status_carries_no_number(self):
        with self.assertRaises(ValidationError):
            metric(status="error", raw_value=0.0, display_value="0.00", reason_codes=["grader_fault"])
        m = metric(status="error", raw_value=None, display_value=None, numerator=None,
                   denominator=None, reason_codes=["grader_fault"])
        self.assertIsNone(m.raw_value)


if __name__ == "__main__":
    unittest.main()
