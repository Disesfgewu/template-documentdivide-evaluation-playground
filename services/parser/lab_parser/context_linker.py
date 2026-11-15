"""Versioned, bounded full-document relation candidates; never a verifier.

Only caller-supplied explicit reference keys are matched. Text, quotes and PDF
content remain data. No cells, assets, facts or source geometry are synthesized.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import stat
from typing import Iterable

REVISION = "explicit-context-linker-1"
SCHEMA = "context-link-input-1"
MAX_BYTES = 16 * 1024 * 1024
RELATION_TYPES = frozenset({"caption_of", "explains", "qualifies", "depicts", "derived_from", "contradicts", "continues", "refers_to", "table_continuation"})
QUALIFIERS = ("chapter", "entity", "year", "unit", "condition")


def _text(value: object, name: str, maximum: int = 128) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid_{name}")


def _integer(value: object, name: str, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid_{name}")


def _tuple(value: object, name: str, maximum: int, cls: type) -> None:
    if type(value) is not tuple or len(value) > maximum or any(type(v) is not cls for v in value):
        raise ValueError(f"invalid_{name}")


def _hash(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class SourceRegion:
    document_id: str
    document_sha256: str
    page_index: int
    bbox: tuple[float, float, float, float]
    kind: str
    quote: str | None

    def __post_init__(self) -> None:
        _text(self.document_id, "document_id")
        if not isinstance(self.document_sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.document_sha256):
            raise ValueError("invalid_document_sha256")
        _integer(self.page_index, "page_index", 0, 1999)
        if type(self.bbox) is not tuple or len(self.bbox) != 4:
            raise ValueError("invalid_bbox")
        if any(type(v) not in (int, float) or not 0 <= v <= 1 or not math.isfinite(v) for v in self.bbox):
            raise ValueError("invalid_bbox")
        if self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("invalid_bbox")
        if not isinstance(self.kind, str) or self.kind not in {"text", "table", "figure", "image", "formula", "page"}:
            raise ValueError("invalid_region_kind")
        if self.kind == "page" and self.bbox != (0, 0, 1, 1):
            raise ValueError("invalid_page_bbox")
        if self.quote is not None and (not isinstance(self.quote, str) or len(self.quote) > 16384):
            raise ValueError("invalid_quote")


@dataclass(frozen=True)
class Anchor:
    """Exact, case-sensitive identifier plus explicitly scoped qualifiers.

    None means unknown. Empty strings are invalid; no fuzzy normalization or
    unit conversion silently changes a reference's scope.
    """
    kind: str
    label: str
    chapter: str | None = None
    entity: str | None = None
    year: str | None = None
    unit: str | None = None
    condition: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"figure", "table", "section", "entity", "definition"}:
            raise ValueError("invalid_anchor_kind")
        _text(self.label, "anchor_label")
        for key in QUALIFIERS:
            if getattr(self, key) is not None:
                _text(getattr(self, key), key)


@dataclass(frozen=True)
class Reference:
    target: Anchor
    relation_type: str = "refers_to"

    def __post_init__(self) -> None:
        if type(self.target) is not Anchor or not isinstance(self.relation_type, str) or self.relation_type not in RELATION_TYPES:
            raise ValueError("invalid_reference")


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    revision: str
    regions: tuple[SourceRegion, ...]
    anchors: tuple[Anchor, ...] = ()
    references: tuple[Reference, ...] = ()

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        _text(self.revision, "evidence_revision")
        _tuple(self.regions, "regions", 32, SourceRegion)
        _tuple(self.anchors, "anchors", 32, Anchor)
        _tuple(self.references, "references", 32, Reference)
        if not self.regions or len(set(self.regions)) != len(self.regions):
            raise ValueError("invalid_regions")
        if len(set(self.anchors)) != len(self.anchors) or len(set(self.references)) != len(self.references):
            raise ValueError("duplicate_metadata")


@dataclass(frozen=True)
class Document:
    project_id: str
    document_id: str
    document_sha256: str
    document_revision: str
    page_count: int
    processed_pages: tuple[int, ...]

    def __post_init__(self) -> None:
        for key in ("project_id", "document_id", "document_revision"):
            _text(getattr(self, key), key)
        if not isinstance(self.document_sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.document_sha256):
            raise ValueError("invalid_document_sha256")
        _integer(self.page_count, "page_count", 1, 2000)
        if type(self.processed_pages) is not tuple or len(self.processed_pages) > 2000 or any(type(p) is not int for p in self.processed_pages):
            raise ValueError("invalid_processed_pages")
        if sorted(self.processed_pages) != list(range(self.page_count)):
            raise ValueError("incomplete_document_manifest")


@dataclass(frozen=True)
class Relation:
    relation_id: str
    relation_type: str
    from_evidence: tuple[str, str]
    to_evidence: tuple[str, str]
    candidate_reason: tuple[str, ...]
    validation_status: str
    source_regions: tuple[SourceRegion, ...]
    target_regions: tuple[SourceRegion, ...]
    linker_revision: str = REVISION
    # These are empty until an independent verifier checks the original regions.
    supporting_regions: tuple[SourceRegion, ...] = ()
    refuting_regions: tuple[SourceRegion, ...] = ()
    reviewer_decision: None = None


@dataclass(frozen=True)
class UnmatchedReference:
    from_evidence: tuple[str, str]
    reference: Reference
    reason: str = "no_explicit_target"


@dataclass(frozen=True)
class LinkResult:
    context_hash: str
    relations: tuple[Relation, ...]
    unmatched_references: tuple[UnmatchedReference, ...]
    evidence_count: int
    duplicate_count: int
    linker_revision: str = REVISION


def link_document(document: Document, evidence: Iterable[Evidence], *, max_records: int = 10000,
                  max_candidates: int = 50000) -> LinkResult:
    """Match explicit references across the entire document, independent of distance.

    Exact repeats from overlapping batches deduplicate by evidence ID/revision.
    Conflicting repeats fail closed. All candidates require independent source
    verification; mismatched qualifiers are retained as conflicting, missing
    qualifiers and multiple compatible destinations as unresolved. Resource
    limits reject the operation rather than return a silently truncated graph.
    """
    if type(document) is not Document:
        raise ValueError("invalid_document")
    _integer(max_records, "max_records", 1, 10000)
    _integer(max_candidates, "max_candidates", 1, 50000)
    records: dict[tuple[str, str], Evidence] = {}
    duplicates = 0
    logical_ids: set[str] = set()
    input_bytes = 0
    for count, item in enumerate(evidence, 1):
        if count > max_records:
            raise ValueError("record_limit_exceeded")
        if type(item) is not Evidence:
            raise ValueError("invalid_evidence")
        for region in item.regions:
            if region.document_id != document.document_id or region.document_sha256 != document.document_sha256:
                raise ValueError("document_identity_mismatch")
            if region.page_index >= document.page_count:
                raise ValueError("page_out_of_range")
        item_bytes = len(json.dumps(asdict(item), ensure_ascii=False, allow_nan=False).encode())
        input_bytes += item_bytes
        if input_bytes > MAX_BYTES:
            raise ValueError("context_size_exceeded")
        key = (item.evidence_id, item.revision)
        if key in records:
            if records[key] != item:
                raise ValueError("conflicting_overlap_identity")
            duplicates += 1
        else:
            # One exact evidence revision per logical ID in this manifest.
            if key[0] in logical_ids:
                raise ValueError("mixed_evidence_revisions")
            logical_ids.add(key[0])
            records[key] = item
    ordered = sorted(records.items())
    context_hash = _hash({"schema": SCHEMA, "linker": REVISION, "document": asdict(document),
                          "evidence": [asdict(item) for _, item in ordered]})
    index: dict[tuple[str, str], list[tuple[tuple[str, str], Anchor]]] = {}
    for key, item in ordered:
        for anchor in item.anchors:
            index.setdefault((anchor.kind, anchor.label), []).append((key, anchor))
    relations: list[Relation] = []
    unmatched: list[UnmatchedReference] = []
    considered = 0
    output_bytes = 0
    for source_key, item in ordered:
        for reference in sorted(item.references, key=lambda r: _hash(asdict(r))):
            targets = index.get((reference.target.kind, reference.target.label), ())
            matches = []
            for target_key, anchor in targets:
                considered += 1
                if considered > max_candidates:
                    raise ValueError("candidate_limit_exceeded")
                if target_key == source_key:
                    continue
                conflicts = tuple(k for k in QUALIFIERS if getattr(reference.target, k) is not None
                                  and getattr(anchor, k) is not None
                                  and getattr(reference.target, k) != getattr(anchor, k))
                missing = tuple(k for k in QUALIFIERS if getattr(reference.target, k) is None
                                or getattr(anchor, k) is None)
                matches.append((target_key, anchor, conflicts, missing))
            if not matches:
                missing_target = UnmatchedReference(source_key, reference)
                output_bytes += len(json.dumps(asdict(missing_target), ensure_ascii=False, allow_nan=False).encode())
                if output_bytes > MAX_BYTES:
                    raise ValueError("relation_output_size_exceeded")
                unmatched.append(missing_target)
                continue
            compatible = {key for key, _, conflicts, _ in matches if not conflicts}
            for target_key, anchor, conflicts, missing in matches:
                reasons = [f"explicit:{anchor.kind}:{anchor.label}"]
                if conflicts:
                    status = "conflicting"
                    reasons.extend(f"mismatch:{k}" for k in conflicts)
                else:
                    reasons.extend(f"unknown:{k}" for k in missing)
                    if len(compatible) > 1:
                        reasons.append("ambiguous_target")
                    status = "unresolved" if missing or len(compatible) > 1 else "candidate"
                identity = {"context": context_hash, "from": source_key, "to": target_key,
                            "reference": asdict(reference), "anchor": asdict(anchor)}
                relation = Relation(_hash(identity), reference.relation_type, source_key, target_key,
                                    tuple(reasons), status, item.regions, records[target_key].regions)
                output_bytes += len(json.dumps(asdict(relation), ensure_ascii=False, allow_nan=False).encode())
                if output_bytes > MAX_BYTES:
                    raise ValueError("relation_output_size_exceeded")
                relations.append(relation)
    result = LinkResult(context_hash, tuple(sorted(relations, key=lambda r: r.relation_id)),
                        tuple(unmatched), len(records), duplicates)
    # Include unmatched references and envelope overhead in the published bound.
    if len(serialize_result(result).encode("utf-8")) > MAX_BYTES:
        raise ValueError("relation_output_size_exceeded")
    return result


def serialize_result(result: LinkResult) -> str:
    return json.dumps({"schema_version": "context-link-result-1", **asdict(result)},
                      sort_keys=True, ensure_ascii=False, allow_nan=False)


def read_manifest(path: Path) -> bytes:
    """Bound reads and reject special files before any potentially blocking read."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("manifest_not_regular_file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("manifest_size_exceeded")
        return raw
    finally:
        os.close(descriptor)


def _object(cls: type, value: object, converters: dict | None = None):
    if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
        raise ValueError(f"invalid_{cls.__name__.lower()}_fields")
    data = dict(value)
    for key, converter in (converters or {}).items():
        data[key] = converter(data[key])
    return cls(**data)


def _array(value: object, converter, limit: int) -> tuple:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid_array")
    return tuple(converter(v) for v in value)


def load_manifest(payload: object) -> tuple[Document, tuple[Evidence, ...]]:
    """Strict internal JSON contract. Unknown/missing fields are rejected."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "document", "evidence"} or payload["schema_version"] != SCHEMA:
        raise ValueError("invalid_manifest_schema")
    anchor = lambda value: _object(Anchor, value)
    reference = lambda value: _object(Reference, value, {"target": anchor})
    region = lambda value: _object(SourceRegion, value, {"bbox": lambda v: _array(v, lambda x: x, 4)})
    evidence = lambda value: _object(Evidence, value, {
        "regions": lambda v: _array(v, region, 32), "anchors": lambda v: _array(v, anchor, 32),
        "references": lambda v: _array(v, reference, 32)})
    document = _object(Document, payload["document"], {"processed_pages": lambda v: _array(v, lambda x: x, 2000)})
    return document, _array(payload["evidence"], evidence, 10000)


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = read_manifest(args.manifest)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
        document, evidence = load_manifest(payload)
        result = link_document(document, evidence)
        print(serialize_result(result))
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        # No raw content/path or exception text leaks into routine diagnostics.
        print('{"error":"invalid_context_manifest"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
