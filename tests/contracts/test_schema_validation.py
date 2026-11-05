"""The exported JSON Schemas, checked by a validator that is not pydantic.

They must accept every valid example (never stricter than the models) and
reject every purely structural invalid example. Rules JSON Schema cannot
express stay in the runtime (pydantic) validators and are listed in
contracts/README.md; those examples are covered by test_contracts only.
"""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas" / "v1"
EXAMPLES = ROOT / "contracts" / "examples"
STRUCTURAL = {"int_type", "bool_type", "missing", "extra_forbidden", "enum", "literal_error",
              "string_pattern_mismatch", "string_too_short", "too_short", "greater_than",
              "greater_than_equal", "less_than_equal"}
# JSON Schema "integer" accepts 3.0 (zero fraction); only strict pydantic rejects it.
NOT_EXPRESSIBLE = {"SourceRegion.page_index_float.json"}


def schema_for(name):
    return json.loads((SCHEMAS / f"{name.split('.', 1)[0]}.schema.json").read_text())


class IndependentSchemaValidationTests(unittest.TestCase):
    def test_schemas_are_valid_2020_12(self):
        for f in sorted(SCHEMAS.glob("*.schema.json")):
            with self.subTest(f.name):
                Draft202012Validator.check_schema(json.loads(f.read_text()))

    def test_no_nonstandard_bound_keywords(self):
        # Bounds must export as minimum/maximum, never pydantic-internal ge/le.
        for f in sorted(SCHEMAS.glob("*.schema.json")):
            text = f.read_text()
            for key in ('"ge"', '"le"', '"gt"', '"lt"'):
                with self.subTest(f.name, key=key):
                    self.assertNotIn(key, text)

    def test_valid_examples_pass(self):
        for f in sorted((EXAMPLES / "valid").glob("*.json")):
            errors = list(Draft202012Validator(schema_for(f.name)).iter_errors(json.loads(f.read_text())))
            with self.subTest(f.name):
                self.assertEqual([e.message for e in errors], [])

    def test_structural_invalid_examples_fail(self):
        expected = json.loads((EXAMPLES / "expected_invalid.json").read_text())
        checked = 0
        for name, want in expected.items():
            if want["type"] not in STRUCTURAL or name in NOT_EXPRESSIBLE:
                continue
            instance = json.loads((EXAMPLES / "invalid" / name).read_text())
            with self.subTest(name):
                self.assertFalse(Draft202012Validator(schema_for(name)).is_valid(instance))
            checked += 1
        self.assertGreaterEqual(checked, 10)


if __name__ == "__main__":
    unittest.main()
