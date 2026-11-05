import json
import math
import unittest
from pathlib import Path

from pydantic import ValidationError

from lab_contracts import export
from lab_contracts.api import ErrorBody, PageRequest
from lab_contracts.base import ATTRIBUTES_MAX_BYTES, Attributes
from lab_contracts.registry import MODELS
from lab_contracts.source import SourceRegion

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
H = "a" * 64


def region(**kw):
    base = dict(document_id="d", document_sha256=H, page_index=0,
                bbox=[0.1, 0.2, 0.5, 0.4], kind="text", quote="q")
    base.update(kw)
    return SourceRegion.model_validate_json(json.dumps(base))


class ExampleTests(unittest.TestCase):
    """Every schema has valid and invalid examples; all are exercised."""

    def _examples(self, kind):
        files = sorted((EXAMPLES / kind).glob("*.json"))
        self.assertTrue(files, f"no {kind} examples")
        for f in files:
            name = f.name.split(".", 1)[0]
            self.assertIn(name, MODELS, f"{f.name}: unknown schema {name}")
            yield f, MODELS[name]

    def test_valid_examples_parse(self):
        for f, model in self._examples("valid"):
            with self.subTest(f.name):
                model.model_validate_json(f.read_text())

    def test_invalid_examples_rejected_for_the_stated_reason(self):
        # Each invalid example must fail with exactly one error, and it must be
        # the one it was written to test (not some incidental mistake).
        expected = json.loads((EXAMPLES / "expected_invalid.json").read_text())
        files = {f.name: model for f, model in self._examples("invalid")}
        self.assertEqual(set(expected), set(files), "expected_invalid.json out of sync")
        for name, model in files.items():
            with self.subTest(name):
                with self.assertRaises(ValidationError) as cm:
                    model.model_validate_json((EXAMPLES / "invalid" / name).read_text())
                errs = cm.exception.errors()
                self.assertEqual(len(errs), 1, errs)
                (err,) = errs
                want = expected[name]
                self.assertEqual(err["type"], want["type"])
                self.assertEqual(".".join(map(str, err["loc"])), want["loc"])
                if "message" in want:
                    self.assertEqual(str(err["ctx"]["error"]), want["message"])

    def test_every_schema_has_both_kinds(self):
        for kind in ("valid", "invalid"):
            have = {f.name.split(".", 1)[0] for f in (EXAMPLES / kind).glob("*.json")}
            self.assertEqual(set(MODELS) - have, set(), f"schemas without {kind} example")

    def test_schemas_not_drifted(self):
        self.assertEqual(export.main(["export", "--check"]), 0)


class SourceRegionTests(unittest.TestCase):
    def test_geometry(self):
        for bbox in ([0.5, 0.2, 0.4, 0.4], [0.1, 0.4, 0.5, 0.4], [-0.1, 0, 0.5, 0.5],
                     [0, 0, 1, 1.0001], [0, 0, 1]):
            with self.subTest(bbox=bbox), self.assertRaises(ValidationError):
                region(bbox=bbox)
        self.assertEqual(region(bbox=[0, 0, 1, 1]).bbox, (0, 0, 1, 1))

    def test_non_finite_rejected(self):
        for v in (math.nan, math.inf):
            with self.subTest(v=v), self.assertRaises(ValidationError):
                SourceRegion(document_id="d", document_sha256=H, page_index=0,
                             bbox=(0.1, 0.1, v, 0.5), kind="text", quote=None)

    def test_negative_page_and_bool_rejected(self):
        for p in (-1, True):
            with self.subTest(p=p), self.assertRaises(ValidationError):
                region(page_index=p)

    def test_document_checks_return_reasons(self):
        r = region(page_index=3)
        self.assertEqual(r.check_against_document(H, 4), [])
        self.assertEqual(r.check_against_document(H, 3), ["page_index_out_of_range"])
        self.assertEqual(r.check_against_document("b" * 64, 3),
                         ["document_sha256_mismatch", "page_index_out_of_range"])
        with self.assertRaises(ValueError):
            r.check_against_document(H, 0)

    def test_resource_id_bounds(self):
        region(document_id="x" * 128)
        for bad in ("", "x" * 129):
            with self.subTest(len=len(bad)), self.assertRaises(ValidationError):
                region(document_id=bad)

    def test_immutable(self):
        with self.assertRaises(ValidationError):
            region().page_index = 2


class ApiContractTests(unittest.TestCase):
    def test_page_request_defaults(self):
        self.assertEqual(PageRequest().page_size, 50)

    def test_error_code_format(self):
        for code in ("", "Bad", "1x", "has space"):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                ErrorBody(code=code, message="m", retryable=False, request_id="r", details={})

    def test_attributes_size_limit(self):
        from pydantic import TypeAdapter
        ta = TypeAdapter(Attributes)
        ta.validate_python({"k": "v" * 100})
        with self.assertRaises(ValidationError):
            ta.validate_python({"k": "v" * ATTRIBUTES_MAX_BYTES})


if __name__ == "__main__":
    unittest.main()
