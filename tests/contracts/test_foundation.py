"""#21 closing checklist, one assertion group per item (SPEC 4.1, 16.1, 16.4)."""
import json
import math
import unittest

from pydantic import TypeAdapter, ValidationError

from lab_contracts.api import ErrorResponse, Page, PageRequest
from lab_contracts.base import ATTRIBUTES_MAX_BYTES, Attributes, ResourceId, Sha256
from lab_contracts.source import SourceRegion

H = "a" * 64
BASE = dict(document_id="doc_1", document_sha256=H, page_index=0, bbox=[0.1, 0.2, 0.5, 0.4],
            kind="text", quote="q")


def region(**kw):
    body = dict(BASE)
    body.update(kw)
    return SourceRegion.model_validate_json(json.dumps(body))


def error_types(fn):
    try:
        fn()
    except ValidationError as exc:
        return [e["type"] for e in exc.errors()]
    raise AssertionError("expected ValidationError")


class PageIndexTypeTests(unittest.TestCase):
    def test_only_json_integers(self):
        self.assertEqual(region(page_index=3).page_index, 3)
        for bad, kind in (("3", "int_type"), (3.0, "int_type"), (True, "int_type"), (-1, "greater_than_equal")):
            with self.subTest(bad=bad):
                self.assertEqual(error_types(lambda: region(page_index=bad)), [kind])


class FiniteNumberTests(unittest.TestCase):
    def test_nan_and_infinity_rejected(self):
        # Python-side input (JSON cannot even encode NaN without the extension).
        for v in (math.nan, math.inf, -math.inf):
            with self.subTest(v=v), self.assertRaises(ValidationError):
                SourceRegion(document_id="d", document_sha256=H, page_index=0,
                             bbox=(0.1, 0.1, v, 0.5), kind="text", quote=None)
        raw = json.dumps(BASE).replace("0.5", "NaN", 1)
        with self.assertRaises(ValidationError):
            SourceRegion.model_validate_json(raw)


class BboxTests(unittest.TestCase):
    def test_boundaries(self):
        region(bbox=[0, 0, 1, 1])                    # inclusive edges
        region(bbox=[0.999999, 0.0, 1.0, 0.000001])  # tiny but positive area
        for bad in ([0.2, 0.2, 0.2, 0.4], [0.1, 0.4, 0.5, 0.4], [-0.000001, 0, 0.5, 0.5],
                    [0, 0, 1.000001, 1], [0.5, 0.1, 0.4, 0.3]):
            with self.subTest(bbox=bad), self.assertRaises(ValidationError):
                region(bbox=bad)

    def test_page_kind_means_whole_page(self):
        region(kind="page", bbox=[0, 0, 1, 1], quote=None)
        with self.assertRaises(ValidationError):
            region(kind="page", bbox=[0, 0, 1, 0.9], quote=None)


class DocumentCheckTests(unittest.TestCase):
    def test_wrong_hash_and_page_out_of_range_are_reasons_not_errors(self):
        r = region(page_index=4)
        self.assertEqual(r.check_against_document(H, 5), [])
        self.assertEqual(r.check_against_document("b" * 64, 4),
                         ["document_sha256_mismatch", "page_index_out_of_range"])


class NullVersusMissingTests(unittest.TestCase):
    def test_quote_required_but_nullable(self):
        self.assertIsNone(region(quote=None).quote)
        body = dict(BASE)
        body.pop("quote")
        self.assertEqual(error_types(lambda: SourceRegion.model_validate_json(json.dumps(body))), ["missing"])

    def test_next_cursor_required_but_nullable(self):
        page = Page[SourceRegion].model_validate_json(json.dumps({"items": [], "page_size": 50, "next_cursor": None}))
        self.assertIsNone(page.next_cursor)
        self.assertEqual(error_types(lambda: Page[SourceRegion].model_validate_json(
            json.dumps({"items": [], "page_size": 50}))), ["missing"])
        self.assertEqual(error_types(lambda: Page[SourceRegion].model_validate_json(
            json.dumps({"items": [], "page_size": 50, "next_cursor": ""}))), ["string_too_short"])


class UnknownFieldTests(unittest.TestCase):
    def test_top_level_unknown_field_rejected(self):
        self.assertEqual(error_types(lambda: region(score=1)), ["extra_forbidden"])
        bad = {"error": {"code": "x", "message": "m", "retryable": False, "request_id": "r",
                         "details": {}, "trace": "t"}}
        self.assertEqual(error_types(lambda: ErrorResponse.model_validate_json(json.dumps(bad))),
                         ["extra_forbidden"])


class IdAndHashLimitTests(unittest.TestCase):
    def test_resource_id_length(self):
        ta = TypeAdapter(ResourceId)
        ta.validate_python("x")
        ta.validate_python("x" * 128)
        for bad in ("", "x" * 129):
            with self.subTest(n=len(bad)), self.assertRaises(ValidationError):
                ta.validate_python(bad)

    def test_sha256_exactly_64_lowercase_hex(self):
        ta = TypeAdapter(Sha256)
        ta.validate_python(H)
        for bad in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63):
            with self.subTest(bad=bad[:6]), self.assertRaises(ValidationError):
                ta.validate_python(bad)


class PaginationTests(unittest.TestCase):
    def test_page_size_bounds_and_default(self):
        self.assertEqual(PageRequest.model_validate_json("{}").page_size, 50)
        self.assertEqual(PageRequest.model_validate_json('{"page_size": 200}').page_size, 200)
        for bad in (0, 201, -1, "50", 50.0):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PageRequest.model_validate_json(json.dumps({"page_size": bad}))

    def test_items_never_exceed_page_size(self):
        item = dict(BASE)
        with self.assertRaises(ValidationError):
            Page[SourceRegion].model_validate_json(json.dumps({"items": [item, item], "page_size": 1,
                                                               "next_cursor": "c"}))


class AttributesTests(unittest.TestCase):
    def test_size_limit_and_json_only(self):
        ta = TypeAdapter(Attributes)
        ta.validate_python({"k": "v" * 100})
        with self.assertRaises(ValidationError):
            ta.validate_python({"k": "v" * ATTRIBUTES_MAX_BYTES})
        for bad in ({"k": {1, 2}}, {"k": object()}, {"k": math.nan}):
            with self.subTest(bad=type(bad["k"]).__name__), self.assertRaises(ValidationError):
                ta.validate_python(bad)


class ImmutabilityScopeTests(unittest.TestCase):
    def test_frozen_is_top_level_only(self):
        r = region()
        with self.assertRaises(ValidationError):
            r.page_index = 2
        # Nested containers are ordinary Python objects: frozen does not make
        # them immutable (documented in contracts/README.md).
        err = ErrorResponse.model_validate_json(json.dumps(
            {"error": {"code": "x", "message": "m", "retryable": False, "request_id": "r", "details": {}}}))
        err.error.details["added"] = 1
        self.assertEqual(err.error.details, {"added": 1})


if __name__ == "__main__":
    unittest.main()
