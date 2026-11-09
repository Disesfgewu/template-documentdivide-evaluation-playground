"""Bounded, deterministic L3 arithmetic; input observations remain unverified data.

This is an internal contract, not a public API or a gold-verification decision.
Only explicit JSON nodes are interpreted. No expressions or Python are executed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Context, Decimal, DecimalException, DivisionByZero, Inexact, InvalidOperation, Overflow, Rounded, ROUND_HALF_EVEN, localcontext

REVISION = "lab-derivation-1"
MAX_NODES = 127
MAX_DEPTH = 16
MAX_OPERANDS = 32
MAX_BYTES = 131072
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UNITS = {"1", "s", "ms", "m", "cm", "kg", "g", "count", "%", "percentage_point"}


class DerivationError(ValueError):
    """A safe error code; messages never echo input content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise DerivationError(code)


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        _fail("invalid_fields")
    return value


def _id(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or value.strip() != value:
        _fail("invalid_id")
    return value


def _hash(value: object) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        _fail("invalid_hash")
    return value


def _json_snapshot(value: object) -> object:
    # Validate bounds and cycles before json.dumps/deepcopy can recurse or allocate
    # an unbounded serialized representation. Shared containers are also rejected.
    seen: set[int] = set()
    stack = [(value, 0)]
    count = 0
    encoded_size = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 8192 or depth > 64:
            _fail("input_limit")
        if type(item) in (dict, list):
            if id(item) in seen:
                _fail("cyclic_or_shared_input")
            seen.add(id(item))
            if len(item) > 1024:
                _fail("input_limit")
            encoded_size += 2 + max(0, len(item) - 1)
            if type(item) is dict:
                if any(type(k) is not str or len(k) > 128 for k in item):
                    _fail("invalid_json")
                encoded_size += len(item) + sum(
                    len(json.encoder.encode_basestring_ascii(k)) for k in item
                )
                stack.extend((v, depth + 1) for v in item.values())
            else:
                stack.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            if len(item) > 8192:
                _fail("input_limit")
            encoded_size += len(json.encoder.encode_basestring_ascii(item))
        elif item is None or type(item) in (bool, int, float):
            if type(item) is int and item.bit_length() > 128:
                _fail("input_limit")
            if type(item) is float and not math.isfinite(item):
                _fail("invalid_json")
            encoded_size += (4 if item is None or item is True else
                             5 if item is False else len(repr(item)))
        else:
            _fail("invalid_json")
        # Account for ASCII escapes and structural punctuation before allocating
        # the complete JSON snapshot (individual strings are already bounded).
        if encoded_size > MAX_BYTES:
            _fail("input_limit")
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                         separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_BYTES:
        _fail("input_limit")
    return json.loads(encoded)


def _number(value: object) -> Decimal:
    # JSON decimal strings avoid binary float ambiguity and prohibit exponents.
    if type(value) is not str or len(value) > 60 or not _DECIMAL.fullmatch(value):
        _fail("invalid_decimal")
    number = Decimal(value)
    _bounded(number)
    return number


def _bounded(value: Decimal) -> None:
    if not value.is_finite() or len(value.as_tuple().digits) > 56:
        _fail("numeric_limit")
    if value != 0 and not -24 <= value.adjusted() <= 24:
        _fail("numeric_limit")
    if value.as_tuple().exponent < -24:
        _fail("numeric_limit")


def _region(value: object) -> dict:
    r = _object(value, {"document_id", "document_sha256", "page_index", "bbox", "kind", "quote"})
    _id(r["document_id"])
    _hash(r["document_sha256"])
    if type(r["page_index"]) is not int or not 0 <= r["page_index"] < 2000:
        _fail("invalid_region")
    b = r["bbox"]
    if type(b) is not list or len(b) != 4 or any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in b
    ) or not (b[0] < b[2] and b[1] < b[3]):
        _fail("invalid_region")
    if r["kind"] not in ("text", "table", "figure", "image", "formula", "page"):
        _fail("invalid_region")
    if r["kind"] == "page" and b != [0, 0, 1, 1]:
        _fail("invalid_region")
    if r["quote"] is not None and type(r["quote"]) is not str:
        _fail("invalid_region")
    return r


def evaluate_derivation(request: object) -> dict:
    """Return detached L3 result/provenance, always ``needs_review``.

    Internal schema v1 uses binary op nodes or named operand leaves. Every
    supplied operand must be used. Unit conversion, estimated values, interval
    propagation and compound dimensions are deliberately rejected in v1.
    """
    req = _object(_json_snapshot(request), {"schema_version", "derivation_id", "operation_id", "operands", "formula"})
    if req["schema_version"] != REVISION:
        _fail("unsupported_schema")
    _id(req["derivation_id"])
    _id(req["operation_id"])
    operands = req["operands"]
    if type(operands) is not list or not 1 <= len(operands) <= MAX_OPERANDS:
        _fail("operand_limit")
    values: dict[str, tuple[Decimal, str]] = {}
    evidence_identity: dict[tuple[str, str], str] = {}
    documents: set[tuple[str, str]] = set()
    for operand in operands:
        o = _object(operand, {"operand_id", "value", "unit", "value_kind", "evidence_ref", "source_regions"})
        name = _id(o["operand_id"])
        if name in values:
            _fail("duplicate_operand")
        if type(o["unit"]) is not str or o["unit"] not in _UNITS:
            _fail("unsupported_unit")
        if o["value_kind"] != "observed_label":
            _fail("unsupported_value_kind")
        e = _object(o["evidence_ref"], {"evidence_id", "revision", "sha256"})
        identity = (_id(e["evidence_id"]), _id(e["revision"]))
        digest = _hash(e["sha256"])
        if identity in evidence_identity and evidence_identity[identity] != digest:
            _fail("conflicting_evidence_identity")
        evidence_identity[identity] = digest
        regions = o["source_regions"]
        if type(regions) is not list or not 1 <= len(regions) <= 16:
            _fail("invalid_region")
        for region in regions:
            r = _region(region)
            documents.add((r["document_id"], r["document_sha256"]))
        values[name] = (_number(o["value"]), o["unit"])
    # MVP is a single immutable PDF; identical logical IDs with new bytes must
    # not be blended either. Document/page existence is a later repository gate.
    if len(documents) != 1:
        _fail("document_scope_mismatch")

    used: set[str] = set()
    node_count = 0

    def visit(node: object, depth: int) -> tuple[Decimal, str]:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_NODES or depth > MAX_DEPTH:
            _fail("formula_limit")
        if type(node) is not dict:
            _fail("invalid_formula")
        if set(node) == {"ref"}:
            name = _id(node["ref"])
            if name not in values:
                _fail("unknown_operand")
            used.add(name)
            return values[name]
        n = _object(node, {"op", "args"})
        op = n["op"]
        if type(op) is not str or op not in ("add", "sub", "mul", "div"):
            _fail("unsupported_operator")
        args = n["args"]
        if type(args) is not list or len(args) != 2:
            _fail("invalid_arity")
        a, au = visit(args[0], depth + 1)
        b, bu = visit(args[1], depth + 1)
        if op in ("add", "sub"):
            if au != bu:
                _fail("unit_mismatch")
            unit = au
            result = a + b if op == "add" else a - b
        elif op == "mul":
            if au != "1" and bu != "1":
                _fail("unsupported_compound_unit")
            unit = bu if au == "1" else au
            result = a * b
        else:
            if b == 0:
                _fail("division_by_zero")
            if bu == "1":
                unit = au
            elif au == bu:
                unit = "1"
            else:
                _fail("unsupported_compound_unit")
            result = a / b
        _bounded(result)
        return result, unit

    context = Context(prec=80, rounding=ROUND_HALF_EVEN, Emin=-100, Emax=100,
                      capitals=1, clamp=0,
                      traps=[DivisionByZero, InvalidOperation, Overflow, Inexact, Rounded])
    try:
        with localcontext(context):
            value, unit = visit(req["formula"], 1)
    except DecimalException as exc:
        raise DerivationError("inexact_or_invalid_arithmetic") from exc
    if used != set(values):
        _fail("unused_operand")
    payload = {"schema_version": REVISION, "layer": "L3", "value_kind": "derived_calculation",
               "status": "needs_review", "value": format(value, "f"), "unit": unit,
               "derivation_id": req["derivation_id"], "operation_id": req["operation_id"],
               "formula": req["formula"], "operands": operands}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    payload["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload
