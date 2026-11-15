"""Hand-calculated internal derivation fixtures; no model/parser quality claims."""
import copy
from decimal import localcontext, ROUND_UP
import hashlib
import json
import unittest
from unittest.mock import patch

from lab_parser.derivations import DerivationError, REVISION, evaluate_derivation


def operand(name, value, unit="ms", page=0):
    return {"operand_id": name, "value": value, "unit": unit,
            "value_kind": "observed_label",
            "evidence_ref": {"evidence_id": "e-" + name, "revision": "r1", "sha256": "a" * 64},
            "source_regions": [{"document_id": "d1", "document_sha256": "b" * 64,
                                "page_index": page, "bbox": [0, 0, 1, 1], "kind": "table", "quote": None}]}


def request(op="sub", a="0.3", b="0.1", au="ms", bu="ms"):
    return {"schema_version": REVISION, "derivation_id": "der1", "operation_id": "op1",
            "operands": [operand("a", a, au), operand("b", b, bu, 247)],
            "formula": {"op": op, "args": [{"ref": "a"}, {"ref": "b"}]}}


class TestDerivations(unittest.TestCase):
    def reject(self, req, code):
        with self.assertRaises(DerivationError) as caught:
            evaluate_derivation(req)
        self.assertEqual(caught.exception.code, code)

    def test_decimal_arithmetic_and_cross_page_provenance(self):
        req = request()
        before = copy.deepcopy(req)
        out = evaluate_derivation(req)
        self.assertEqual((out["value"], out["unit"]), ("0.2", "ms"))
        self.assertEqual(out["status"], "needs_review")
        self.assertEqual((out["layer"], out["value_kind"]), ("L3", "derived_calculation"))
        self.assertEqual(out["operands"][1]["source_regions"][0]["page_index"], 247)
        self.assertEqual(req, before)
        out["operands"][0]["source_regions"][0]["bbox"][0] = 0.25
        self.assertEqual(req, before)

    def test_hash_is_deterministic_and_covers_provenance(self):
        req = request()
        out = evaluate_derivation(req)
        digest = out.pop("content_sha256")
        self.assertEqual(digest, hashlib.sha256(json.dumps(out, sort_keys=True, ensure_ascii=True,
                                                        separators=(",", ":")).encode("ascii")).hexdigest())
        self.assertEqual(digest, evaluate_derivation(req)["content_sha256"])
        req["operands"][0]["evidence_ref"]["revision"] = "r2"
        self.assertNotEqual(digest, evaluate_derivation(req)["content_sha256"])

    def test_four_operators(self):
        for op, expected in [("add", "15"), ("sub", "9"), ("mul", "36"), ("div", "4")]:
            with self.subTest(op=op):
                req = request(op, "12", "3", "1", "1")
                self.assertEqual(evaluate_derivation(req)["value"], expected)

    def test_units_scalar_products_and_ratios(self):
        for op, au, bu, unit in [("mul", "1", "ms", "ms"), ("mul", "ms", "1", "ms"),
                                 ("div", "ms", "1", "ms"), ("div", "ms", "ms", "1")]:
            with self.subTest(op=op, au=au, bu=bu):
                self.assertEqual(evaluate_derivation(request(op, "12", "3", au, bu))["unit"], unit)

    def test_no_implicit_conversion_or_percent_conflation(self):
        for au, bu in [("ms", "s"), ("%", "percentage_point"), ("cm", "m")]:
            self.reject(request(au=au, bu=bu), "unit_mismatch")
        self.reject(request("mul"), "unsupported_compound_unit")
        self.reject(request("div", au="1", bu="s"), "unsupported_compound_unit")
        self.reject(request(au="USD"), "unsupported_unit")

    def test_context_isolated_and_nonterminating_division_rejected(self):
        expected = evaluate_derivation(request())
        with localcontext() as ctx:
            ctx.prec = 1
            ctx.rounding = ROUND_UP
            self.assertEqual(evaluate_derivation(request()), expected)
        self.reject(request("div", "1", "3"), "inexact_or_invalid_arithmetic")
        self.reject(request("div", "1", "0"), "division_by_zero")

    def test_no_short_circuit_provenance_loss(self):
        out = evaluate_derivation(request("mul", "0", "4", "1", "ms"))
        self.assertEqual(out["value"], "0")
        self.assertEqual(len(out["operands"]), 2)
        out = evaluate_derivation(request("sub", "2", "2"))
        self.assertEqual(len(out["operands"]), 2)

    def test_numeric_grammar_and_types(self):
        for bad in ["NaN", "Infinity", "1e3", "+1", " 1", "1_0", "١", ".1", "01", 1, 1.2, True, None]:
            with self.subTest(value=bad):
                req = request()
                req["operands"][0]["value"] = bad
                self.reject(req, "invalid_decimal")

    def test_numeric_resource_limits_before_and_after_arithmetic(self):
        for bad in ["1" * 61, "0." + "0" * 24 + "1", "1" + "0" * 25]:
            req = request(a=bad)
            with self.assertRaises(DerivationError):
                evaluate_derivation(req)
        self.reject(request("mul", "1000000000000000000000000", "10", "1", "1"), "numeric_limit")

    def test_unknown_fields_and_operators_never_execute(self):
        for op in ["pow", "__import__('os').system('false')", ["add"]]:
            self.reject(request(op=op), "unsupported_operator")
        req = request()
        req["formula"]["code"] = "open('/tmp/nope','w')"
        self.reject(req, "invalid_fields")
        req = request()
        req["unexpected"] = True
        self.reject(req, "invalid_fields")
        req = request()
        req["formula"] = "a+b"
        self.reject(req, "invalid_formula")

    def test_internal_contract_boundary_and_arity(self):
        req = request()
        req["schema_version"] = "1.0"
        self.reject(req, "unsupported_schema")
        for formula in [{"const": 1}, {"op": "neg", "args": [{"ref": "a"}]},
                        {"op": "max", "args": [{"ref": "a"}, {"ref": "b"}]}]:
            req = request()
            req["formula"] = formula
            with self.assertRaises(DerivationError):
                evaluate_derivation(req)
        for args in [[], [{"ref": "a"}], {}, [1, 2, 3]]:
            req = request()
            req["formula"]["args"] = args
            self.reject(req, "invalid_arity")
        self.reject(request("div", "1", "-0"), "division_by_zero")

    def test_missing_duplicate_and_unused_operands(self):
        req = request()
        req["formula"]["args"][0]["ref"] = "missing"
        self.reject(req, "unknown_operand")
        req = request()
        req["operands"][1]["operand_id"] = "a"
        self.reject(req, "duplicate_operand")
        req = request()
        req["formula"] = {"ref": "a"}
        self.reject(req, "unused_operand")

    def test_evidence_identity_and_document_scope(self):
        req = request()
        req["operands"][1]["evidence_ref"] = {"evidence_id": "e-a", "revision": "r1", "sha256": "c" * 64}
        self.reject(req, "conflicting_evidence_identity")
        for field in ["document_id", "document_sha256"]:
            req = request()
            req["operands"][1]["source_regions"][0][field] = "d2" if field == "document_id" else "c" * 64
            self.reject(req, "document_scope_mismatch")

    def test_source_region_validation(self):
        for field, value in [("bbox", [0, 0, 0, 1]), ("bbox", [False, 0, 1, 1]),
                             ("page_index", True), ("page_index", 2000), ("kind", "inference"), ("quote", 2)]:
            req = request()
            req["operands"][0]["source_regions"][0][field] = value
            self.reject(req, "invalid_region")
        req = request()
        req["operands"][0]["source_regions"] = []
        self.reject(req, "invalid_region")

    def test_estimated_input_not_falsely_exact(self):
        req = request()
        req["operands"][0]["value_kind"] = "estimated_from_plot"
        self.reject(req, "unsupported_value_kind")

    def test_formula_depth_bound(self):
        req = request()
        node = {"ref": "a"}
        for _ in range(16):
            node = {"op": "add", "args": [node, {"ref": "b"}]}
        req["formula"] = node
        self.reject(req, "formula_limit")

    def test_formula_node_bound(self):
        def tree(depth):
            if depth == 0:
                return {"ref": "a"}
            return {"op": "add", "args": [tree(depth - 1), tree(depth - 1)]}
        req = request()
        req["formula"] = tree(7)
        self.reject(req, "formula_limit")

    def test_cycles_shared_nodes_and_nonjson_types_fail_safely(self):
        req = request()
        req["formula"]["args"][0] = req
        self.reject(req, "cyclic_or_shared_input")
        req = request()
        req["formula"]["args"][1] = req["formula"]["args"][0]
        self.reject(req, "cyclic_or_shared_input")
        req = request()
        req["operands"][0]["value"] = object()
        self.reject(req, "invalid_json")

    def test_input_size_and_operand_bound(self):
        req = request()
        req["operands"] = [operand(str(i), "1") for i in range(33)]
        self.reject(req, "operand_limit")
        req = request()
        req["operands"][0]["source_regions"][0]["quote"] = "x" * 8193
        self.reject(req, "input_limit")

    def test_cumulative_input_bound_precedes_full_serialization(self):
        req = request()
        req["oversized"] = ["\uffff" * 8192 for _ in range(4)]
        with patch("lab_parser.derivations.json.dumps", side_effect=AssertionError("serialized oversized input")) as encode:
            self.reject(req, "input_limit")
        encode.assert_not_called()
