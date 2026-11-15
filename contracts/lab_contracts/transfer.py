"""Bounded private benchmark transfer; integrity is not authorization or freeze approval.

The JSON envelope contains six logical files, never filesystem paths. The
participant projection is a separate allowlist, not a filtered private archive.
"""
from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal
import os
import stat
import sys
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .base import Contract, ResourceId, Sha256
from .benchmark import (BenchmarkManifest, EvidenceUnit, Question, SemanticAnswer,
                        SourceAnswer, TextualAnswer, ValidationRecord,
                        check_manifest, validate_bundle)
from .method import CANONICAL_JSON_VERSION, StrictJSONError, canonical_json, strict_loads

FORMAT = "lab-benchmark-transfer-1"
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 20_000             # across all six files
MAX_DEPTH = 64
MAX_NODES = 500_000
MODELS = {"questions": Question, "semantic_answers": SemanticAnswer,
          "textual_answers": TextualAnswer, "source_answers": SourceAnswer,
          "evidence": EvidenceUnit, "validations": ValidationRecord}
KEYS = {**{k: "question_id" for k in list(MODELS)[:4]},
        "evidence": "evidence_id", "validations": "validation_id"}


class TransferError(ValueError):
    """Stable sanitized failure code; never includes document/gold content."""


class TransferMetadata(Contract):
    benchmark_id: ResourceId
    revision_id: ResourceId
    document_id: ResourceId
    document_sha256: Sha256
    page_count: Annotated[int, Field(ge=1, le=2000)]


def _bounded_tree(value):
    """Validate plain JSON types and resource bounds before canonical allocation."""
    stack = [(value, 0)]
    nodes = text_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise TransferError("structure_limit")
        if isinstance(item, str):
            # Check character count before making a UTF-8 copy of a caller string.
            if len(item) > MAX_BYTES:
                raise TransferError("payload_too_large")
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > MAX_BYTES:
                raise TransferError("payload_too_large")
        elif type(item) is dict:
            if len(item) > MAX_NODES - nodes:
                raise TransferError("structure_limit")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TransferError("invalid_json_value")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif type(item) is list:
            if len(item) > MAX_NODES - nodes:
                raise TransferError("structure_limit")
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (bool, int, float):
            # strict_loads returns Decimal for noninteger JSON tokens.
            if not isinstance(item, Decimal):
                raise TransferError("invalid_json_value")


def _encode(value):
    _bounded_tree(value)
    try:
        raw = canonical_json(value)
    except (ValueError, TypeError, ArithmeticError, UnicodeError, RecursionError):
        raise TransferError("invalid_json_value") from None
    if len(raw) > MAX_BYTES:
        raise TransferError("payload_too_large")
    return raw


def _load(body):
    if type(body) is not bytes:
        raise TransferError("bytes_required")
    if len(body) > MAX_BYTES:
        raise TransferError("payload_too_large")
    # Reject pathological JSON depth before json.loads allocates a nested tree.
    quoted = escaped = False
    depth = tokens = 0
    try:
        text = body.decode("utf-8")
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
                tokens += 1
            elif char in ",:":
                tokens += 1
            elif char in "[{":
                tokens += 1
                depth += 1
                if depth > MAX_DEPTH:
                    raise TransferError("structure_limit")
            elif char in "]}":
                depth -= 1
            if tokens > MAX_NODES:
                raise TransferError("structure_limit")
        value = strict_loads(text)
        _bounded_tree(value)
        return value
    except (UnicodeError, StrictJSONError):
        raise TransferError("invalid_json") from None


def _fields(obj, fields):
    if type(obj) is not dict or set(obj) != set(fields):
        raise TransferError("invalid_fields")


def _validation_json(value):
    """Preserve fractional-token types for strict integer model validation.

    Hash canonicalization intentionally equates 1 and 1.0, but schema validation
    must distinguish them. Decimal spelling retains a decimal point/exponent.
    """
    if isinstance(value, Decimal):
        token = str(value)
        return token if any(char in token for char in ".eE") else token + ".0"
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_validation_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(canonical_json(key).decode() + ":" + _validation_json(item)
                              for key, item in value.items()) + "}"
    return canonical_json(value).decode()


def _model(model, value):
    _bounded_tree(value)
    try:
        result = model.model_validate_json(_validation_json(value))
        # Canonical output may turn an enormous strict integer into exponent
        # notation. Reject such a snapshot rather than emit a non-importable
        # archive or weaken the existing strict-integer contract.
        model.model_validate_json(_encode(value))
        return result
    except ValidationError:
        raise TransferError("schema_invalid") from None


def _exact_bounds(files):
    """Check constrained numeric source values before binary64 model rounding.

    The merged models store floats; raw snapshots keep exact JSON decimals, so
    a value just above 1 must not be accepted as a rounded 1 in source geometry
    or confidence. This also preserves exact interval ordering.
    """
    for unit in files["evidence"]:
        confidence = unit["ocr_confidence"]
        if confidence is not None and not 0 <= confidence <= 1:
            raise TransferError("numeric_bounds_invalid")
        for region in unit["regions"]:
            x0, y0, x1, y1 = region["bbox"]
            if not all(0 <= coordinate <= 1 for coordinate in (x0, y0, x1, y1)):
                raise TransferError("numeric_bounds_invalid")
            if not (x0 < x1 and y0 < y1):
                raise TransferError("numeric_bounds_invalid")
            if region["kind"] == "page" and (x0, y0, x1, y1) != (0, 0, 1, 1):
                raise TransferError("numeric_bounds_invalid")
    for validation in files["validations"]:
        confidence = validation["confidence"]
        if confidence is not None and not 0 <= confidence <= 1:
            raise TransferError("numeric_bounds_invalid")
    for answer in files["semantic_answers"]:
        if any(fact["weight"] <= 0 for fact in answer["required_facts"]):
            raise TransferError("numeric_bounds_invalid")
    for answer in files["textual_answers"]:
        for check in answer["checks"]:
            interval = check["interval"]
            if interval is not None and interval[0] > interval[1]:
                raise TransferError("numeric_bounds_invalid")
            if any(check[field] is not None and check[field] < 0
                   for field in ("absolute_tolerance", "relative_tolerance")):
                raise TransferError("numeric_bounds_invalid")


def _validate(metadata, files, source_manifest):
    meta = _model(TransferMetadata, metadata)
    _fields(files, MODELS)
    if any(type(records) is not list for records in files.values()):
        raise TransferError("invalid_files")
    if sum(len(records) for records in files.values()) > MAX_RECORDS:
        raise TransferError("record_limit")
    models = {}
    normalized = {}
    for name, model in MODELS.items():
        records = [_model(model, record) for record in files[name]]
        ids = [getattr(record, KEYS[name]) for record in records]
        if len(set(ids)) != len(ids):
            raise TransferError("duplicate_record_id")
        models[name] = records
        # Preserve original JSON numbers/fields; model conversion must not rewrite
        # source evidence. Sorting changes file order only, never nested records.
        normalized[name] = sorted(files[name], key=lambda item: item[KEYS[name]])
    _exact_bounds(files)
    for unit in models["evidence"]:
        for region in unit.regions:
            if region.document_id != meta.document_id or region.check_against_document(
                    meta.document_sha256, meta.page_count):
                raise TransferError("source_identity_mismatch")
    if validate_bundle(models["questions"], models["semantic_answers"],
                       models["textual_answers"], models["source_answers"],
                       {r.evidence_id: r for r in models["evidence"]},
                       {r.validation_id: r for r in models["validations"]}):
        raise TransferError("bundle_invalid")
    if source_manifest is not None:
        manifest = _model(BenchmarkManifest, source_manifest)
        for field in TransferMetadata.model_fields:
            if getattr(manifest, field) != getattr(meta, field):
                raise TransferError("manifest_identity_mismatch")
        if check_manifest(manifest, models["questions"]):
            raise TransferError("manifest_counts_mismatch")
        for name in list(MODELS)[:4]:
            if getattr(manifest.file_hashes, name) != _inventory(normalized[name])["sha256"]:
                raise TransferError("source_manifest_hash_mismatch")
    return normalized


def _inventory(records):
    body = _encode(records)
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "records": len(records)}


def export_archive(source: bytes) -> bytes:
    """Validate an operator source JSON and return deterministic private bytes.

    Source keys: metadata, files (six lists), source_manifest (null or an
    existing BenchmarkManifest). No freeze/verification status is invented.
    """
    obj = _load(source)
    _fields(obj, ("metadata", "files", "source_manifest"))
    files = _validate(obj["metadata"], obj["files"], obj["source_manifest"])
    archive = {"format": FORMAT, "canonicalization": CANONICAL_JSON_VERSION,
               "metadata": obj["metadata"], "files": files,
               "inventory": {name: _inventory(records) for name, records in files.items()},
               "source_manifest": obj["source_manifest"]}
    return _encode(archive)


def import_archive(body: bytes) -> dict:
    """Verify the whole archive before returning a fresh operator source value.

    Returned JSON is a detached mutable snapshot; revalidation is required
    before later use. File hashes detect corruption, not malicious authorship.
    """
    obj = _load(body)
    _fields(obj, ("format", "canonicalization", "metadata", "files", "inventory", "source_manifest"))
    if obj["format"] != FORMAT or obj["canonicalization"] != CANONICAL_JSON_VERSION:
        raise TransferError("unsupported_format")
    _fields(obj["files"], MODELS)
    _fields(obj["inventory"], MODELS)
    for name, records in obj["files"].items():
        if type(records) is not list:
            raise TransferError("invalid_files")
        expected = _inventory(records)
        actual = obj["inventory"][name]
        _fields(actual, expected)
        # bool compares equal to integer in Python, so explicitly reject it.
        if type(actual["bytes"]) is not int or type(actual["records"]) is not int or actual != expected:
            raise TransferError("integrity_mismatch")
    _validate(obj["metadata"], obj["files"], obj["source_manifest"])
    return {name: obj[name] for name in ("metadata", "files", "source_manifest")}


def participant_questions(body: bytes) -> bytes:
    """Minimal explicit public allowlist; no gold, evidence, quotas or hashes.

    The caller must authorize the participant and select an approved frozen
    revision. This codec cannot establish those service-level facts.
    """
    source = import_archive(body)
    records = sorted(source["files"]["questions"], key=lambda q: q["ordinal"])
    return _encode([{key: record[key] for key in ("question_id", "question_text", "language")}
                    for record in records])


def _read_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TransferError("regular_file_required")
        if info.st_size > MAX_BYTES:
            raise TransferError("payload_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise TransferError("payload_too_large")
        return body
    finally:
        os.close(fd)


def _write_file(path, body):
    # Exclusive creation protects both existing files and final symlinks. Parent
    # directories must be operator-controlled; this is not a filesystem sandbox.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    else:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "import", "check", "project"))
    parser.add_argument("input")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command != "check" and args.output is None:
        parser.error("--output is required")
    if args.command == "check" and args.output is not None:
        parser.error("check does not write an output file")
    try:
        body = _read_file(args.input)
        if args.command == "export":
            result = export_archive(body)
        elif args.command == "project":
            result = participant_questions(body)
        else:
            source = import_archive(body)
            result = _encode(source)
        if args.command != "check":
            _write_file(args.output, result)
        print('{"status":"passed","operation":"' + args.command + '"}')
        return 0
    except TransferError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print("file_io_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
