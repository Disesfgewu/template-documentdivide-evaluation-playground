import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from lab_contracts import openapi
from lab_contracts.registry import MODELS

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md").read_text(encoding="utf-8")
DOC = json.loads((ROOT / "contracts" / "openapi" / "method-service.v1.json").read_text())
SCHEMAS = ROOT / "contracts" / "schemas" / "v1"
EXAMPLES = ROOT / "contracts" / "examples"


def refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


class OpenApiTests(unittest.TestCase):
    def test_paths_match_spec_7_2(self):
        section = SPEC[SPEC.index("### 7.2 HTTP 方法服務"):SPEC.index("prepare 請求包含")]
        spec_eps = {(m.lower(), p) for m, p in re.findall(r"^\| (GET|POST) \| (/v1/\S+) \|", section, re.M)}
        doc_eps = {(m, p) for p, ops in DOC["paths"].items() for m in ops}
        self.assertEqual(doc_eps, spec_eps)
        self.assertEqual(len(spec_eps), 5)

    def test_every_ref_resolves(self):
        names = set(DOC["components"]["schemas"])
        for r in refs(DOC):
            self.assertTrue(r.startswith("#/components/schemas/"), r)
            self.assertIn(r.rsplit("/", 1)[1], names)

    def test_generated_document_is_current(self):
        self.assertEqual(json.loads(json.dumps(openapi.build(), sort_keys=True)),
                         json.loads(json.dumps(DOC, sort_keys=True)))


class IndependentSchemaValidationTests(unittest.TestCase):
    """The exported JSON Schemas, checked by a validator that is not pydantic,
    must accept every valid example (they must not be stricter than the
    models). Cross-field rules live in model validators and are not
    expressible in JSON Schema, so invalid examples are only checked by
    pydantic (test_contracts)."""

    def test_valid_examples_pass_exported_schemas(self):
        for f in sorted((EXAMPLES / "valid").glob("*.json")):
            name = f.name.split(".", 1)[0]
            schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
            Draft202012Validator.check_schema(schema)
            errors = list(Draft202012Validator(schema).iter_errors(json.loads(f.read_text())))
            with self.subTest(f.name):
                self.assertEqual([e.message for e in errors], [])

    def test_structural_invalid_examples_fail_exported_schemas(self):
        # Errors that are purely structural (types, enums, patterns, bounds,
        # missing/extra fields) must be caught by the schema too.
        expected = json.loads((EXAMPLES / "expected_invalid.json").read_text())
        structural = {"int_type", "bool_type", "missing", "extra_forbidden", "enum", "literal_error",
                      "string_pattern_mismatch", "string_too_short", "too_short", "greater_than",
                      "greater_than_equal", "less_than_equal"}
        # JSON Schema's "integer" accepts 3.0 (zero fraction); only pydantic's
        # strict mode rejects it. Documented in contracts/README.md.
        json_schema_cannot_express = {"Question.ordinal_float.json", "SourceRegion.page_index_float.json"}
        checked = 0
        for name, want in expected.items():
            if want["type"] not in structural or name in json_schema_cannot_express:
                continue
            schema = json.loads((SCHEMAS / f"{name.split('.', 1)[0]}.schema.json").read_text())
            instance = json.loads((EXAMPLES / "invalid" / name).read_text())
            with self.subTest(name):
                self.assertFalse(Draft202012Validator(schema).is_valid(instance))
            checked += 1
        self.assertGreater(checked, 40)


if __name__ == "__main__":
    unittest.main()
