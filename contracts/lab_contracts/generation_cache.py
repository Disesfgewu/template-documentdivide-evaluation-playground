"""Deterministic question generation cache identity and provenance (SPEC 6, 10.2, 16, 19.6).

This module validates and canonicalizes the inputs that define a question
generation cache key. It is a pure, stateless identity/provenance boundary:
it does not implement durable cache storage, trace/operation persistence,
provider calls, semantic/source verification or benchmark freeze.

Untrusted text in evidence, quotes or generation settings is treated strictly
as opaque data; it cannot alter cache policy or bypass required provenance.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import math
from typing import Annotated, Any, Literal, Optional

from pydantic import Field, StringConstraints, model_validator

from .base import Contract, FiniteFloat, ResourceId, Sha256, check_unique
from .benchmark import EvidenceUnit, Modality, QuotaBucket, ReasoningType
from .method import CANONICAL_JSON_VERSION, StrictJSONError, canonical_json, strict_loads
from .parse import ParseManifest
from .source import RegionKind, SourceRegion

GENERATION_CACHE_KEY_VERSION = "generation-cache-key-1"

MAX_DOCUMENTS = 128
MAX_EVIDENCE_UNITS = 256
MAX_REGIONS_PER_UNIT = 128
MAX_ASSETS_PER_UNIT = 128
MAX_PARSE_MANIFESTS = 64
MAX_VISUAL_ARTIFACTS = 256
MAX_INPUT_ARTIFACTS = 128
MAX_RELATIONS = 256
MAX_REGION_REFS = 128
MAX_QUESTION_COUNT = 2_000
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
MAX_UNIT_CONTENT_BYTES = 1024 * 1024
MAX_TOTAL_CONTENT_BYTES = 8 * 1024 * 1024
MAX_PARAMETER_BYTES = 16 * 1024
MAX_PARAMETER_DEPTH = 8
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

_UNPINNED = {"unknown", "latest", "unpinned", "main", "master", "head"}
_VISUAL_REGION_KINDS = frozenset({
    RegionKind.table,
    RegionKind.figure,
    RegionKind.image,
    RegionKind.page,
})
_VISUAL_MODALITIES = frozenset({Modality.table, Modality.image})
_VISUAL_REASONING_TYPES = frozenset({
    ReasoningType.diagram_relation,
    ReasoningType.image_text_integration,
})

NonWhiteId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"\S")]
NonWhiteLabel = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"\S")]
MimeType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    ),
]


def _is_unpinned(value: str) -> bool:
    cleaned = value.strip().lower()
    return (
        not cleaned
        or cleaned in _UNPINNED
        or cleaned.endswith(("@unknown", "@latest", ":latest", ":unknown"))
    )


def _require_pinned(value: str, field_name: str) -> str:
    if _is_unpinned(value):
        raise ValueError(f"{field_name} must be a pinned revision, not unknown or floating")
    return value


def _check_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PARAMETER_DEPTH:
        raise ValueError(f"generation parameters exceed max depth {MAX_PARAMETER_DEPTH}")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        try:
            f = float(value)
        except OverflowError:
            raise ValueError("generation parameter number out of range") from None
        if not math.isfinite(f):
            raise ValueError("generation parameter number out of range")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("generation parameters must not contain NaN or Infinity")
        try:
            f = float(value)
        except (ValueError, OverflowError):
            raise ValueError("generation parameter number out of range") from None
        if not math.isfinite(f) or (f == 0.0 and value != 0):
            raise ValueError("generation parameter number out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("generation parameters must not contain NaN or Infinity")
        return value
    if type(value) is list:
        return [_check_json_value(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k.strip() or len(k) > 128:
                raise ValueError("generation parameter keys must be non-empty strings <= 128 chars")
            out[k] = _check_json_value(v, depth=depth + 1)
        return out
    raise ValueError(f"unsupported generation parameter type: {type(value).__name__}")


def _check_lossless_numbers(raw_obj: Any, model_obj: Any, path: str = "") -> None:
    """Reject any non-parameter JSON number token whose exact decimal value
    would be rounded by binary64 float validation."""
    if isinstance(raw_obj, dict) and isinstance(model_obj, dict):
        for k, raw_v in raw_obj.items():
            sub_path = f"{path}.{k}" if path else k
            if sub_path == "generation_config.parameters":
                # parameters preserves exact Decimal values directly.
                continue
            if k in model_obj:
                _check_lossless_numbers(raw_v, model_obj[k], sub_path)
    elif isinstance(raw_obj, list) and isinstance(model_obj, (list, tuple)):
        for idx, (raw_v, mod_v) in enumerate(zip(raw_obj, model_obj)):
            _check_lossless_numbers(raw_v, mod_v, f"{path}[{idx}]")
    elif isinstance(raw_obj, (Decimal, int)) and not isinstance(raw_obj, bool) and isinstance(model_obj, float):
        if Decimal(repr(model_obj)) != Decimal(raw_obj):
            raise ValueError(f"{path} numeric value {raw_obj} loses precision in binary64 float")


_RESERVED_PARAMETER_KEYS = frozenset({
    "generator_config_version",
    "question_generator_model",
    "question_generator_prompt_version",
    "output_schema_version",
    "language",
    "max_output_tokens",
    "temperature",
    "seed",
    "vision_reader_model",
    "vision_prompt_version",
    "verifier_revision",
    "selection_rule_version",
    "quota_bucket",
    "reasoning_type",
    "modality_requirements",
    "question_count",
})


class SourceDocumentScope(Contract):
    """Original PDF identity and physical page count (SPEC 4.1, 16.1)."""

    document_id: NonWhiteId
    document_sha256: Sha256
    page_count: Annotated[int, Field(ge=1, le=2000)]


class ParseProvenance(Contract):
    """Pinned parser/OCR provenance for reproducibility (SPEC 6, 19.2, 19.6)."""

    parse_run_id: NonWhiteId
    document_id: NonWhiteId
    parser_name: NonWhiteId
    package_or_service_version: NonWhiteId
    adapter_revision: NonWhiteId
    output_schema: NonWhiteId
    model_revisions: list[tuple[NonWhiteLabel, NonWhiteId, Optional[Sha256]]] = Field(default_factory=list)
    requested_profile: NonWhiteId
    effective_profile: NonWhiteId
    fallback_reason: Optional[NonWhiteLabel] = None
    ocr_languages: list[Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]]
    page_selection: Annotated[list[Annotated[int, Field(ge=0, le=1999)]], Field(min_length=1, max_length=2000)]
    render_dpi: Annotated[int, Field(ge=72, le=600)]
    render_max_megapixels: Annotated[FiniteFloat, Field(gt=0, le=16)]
    postprocess_flags: list[NonWhiteLabel]
    reproducible: Literal[True]

    @model_validator(mode="after")
    def _validate_provenance(self):
        for name in ("parser_name", "package_or_service_version", "adapter_revision",
                     "output_schema", "requested_profile", "effective_profile"):
            _require_pinned(getattr(self, name), name)
        if (self.requested_profile != self.effective_profile) != (self.fallback_reason is not None):
            raise ValueError("fallback_reason is required_exactly when effective_profile differs from requested")
        check_unique(self.page_selection, "page_selection")
        check_unique(self.ocr_languages, "ocr_language")
        check_unique(self.postprocess_flags, "postprocess_flag")
        check_unique([m[0] for m in self.model_revisions], "model_revision name")
        for _, rev, _ in self.model_revisions:
            _require_pinned(rev, "model_revision")
        return self

    @classmethod
    def from_parse_manifest(
        cls,
        *,
        parse_run_id: str,
        document_id: str,
        manifest: ParseManifest,
    ) -> "ParseProvenance":
        """Extract pinned cache-relevant fields from a ParseManifest, excluding
        wall-clock timestamps, cost and transient deployment IDs."""
        if not isinstance(manifest, ParseManifest):
            raise TypeError("manifest must be a validated ParseManifest")
        if not manifest.reproducible:
            raise ValueError("generation cache requires a reproducible ParseManifest")
        return cls(
            parse_run_id=parse_run_id,
            document_id=document_id,
            parser_name=manifest.parser_name,
            package_or_service_version=manifest.package_or_service_version,
            adapter_revision=manifest.adapter_revision,
            output_schema=manifest.output_schema,
            model_revisions=[
                (m.name, m.revision, m.checksum)
                for m in sorted(manifest.model_revisions, key=lambda item: item.name)
            ],
            requested_profile=manifest.requested_profile,
            effective_profile=manifest.effective_profile,
            fallback_reason=manifest.fallback_reason,
            ocr_languages=sorted(manifest.ocr_languages),
            page_selection=sorted(manifest.page_selection),
            render_dpi=manifest.render_settings.dpi,
            render_max_megapixels=manifest.render_settings.max_megapixels,
            postprocess_flags=sorted(manifest.postprocess_flags),
            reproducible=True,
        )


class ArtifactDigest(Contract):
    """Immutable committed artifact reference (SPEC 10.1, 10.2)."""

    artifact_id: NonWhiteId
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES)]
    mime_type: MimeType


class ContextSlot(Contract):
    """One slot in the ordered generation context manifest (SPEC 10.2)."""

    slot_index: Annotated[int, Field(ge=0, le=MAX_EVIDENCE_UNITS - 1)]
    evidence_id: NonWhiteId
    content_mode: Literal["raw", "normalized"]
    truncated: bool
    included_char_start: Optional[Annotated[int, Field(ge=0)]] = None
    included_char_end: Optional[Annotated[int, Field(gt=0)]] = None
    included_content_sha256: Sha256
    truncation_reason: Optional[NonWhiteLabel] = None

    @model_validator(mode="after")
    def _truncation_consistency(self):
        has_start = self.included_char_start is not None
        has_end = self.included_char_end is not None
        if has_start != has_end:
            raise ValueError("included_char_start and included_char_end must both be set or both be null")
        if has_start and self.included_char_start >= self.included_char_end:
            raise ValueError("included_char_start must be < included_char_end")
        if self.truncated:
            if not has_start or self.truncation_reason is None:
                raise ValueError("truncated context slot requires char bounds and truncation_reason")
        elif self.truncation_reason is not None:
            raise ValueError("untruncated context slot must not set truncation_reason")
        return self


ContextRelationType = Literal[
    "continues",
    "defines",
    "refers_to",
    "same_entity",
    "similar_but_different",
    "table_continuation",
    "caption_of",
    "explains",
    "qualifies",
    "depicts",
    "derived_from",
    "contradicts",
]


class ContextRelationRef(Contract):
    """Verified cross-text or cross-page relation in context (SPEC 4.2, 19.4)."""

    relation_id: NonWhiteId
    relation_type: ContextRelationType
    from_evidence_id: NonWhiteId
    to_evidence_id: NonWhiteId
    linker_revision: NonWhiteId
    validation_status: Literal["verified"]

    @model_validator(mode="after")
    def _validate_relation(self):
        _require_pinned(self.linker_revision, "linker_revision")
        if self.from_evidence_id == self.to_evidence_id:
            raise ValueError("relation endpoints must be distinct evidence units")
        return self


class ContextManifest(Contract):
    """Actual ordered context manifest and input artifacts (SPEC 10.2, 19.6)."""

    manifest_id: NonWhiteId
    linker_revision: NonWhiteId
    slots: Annotated[list[ContextSlot], Field(min_length=1, max_length=MAX_EVIDENCE_UNITS)]
    relations: Annotated[list[ContextRelationRef], Field(max_length=MAX_RELATIONS)] = Field(default_factory=list)
    input_artifacts: Annotated[list[ArtifactDigest], Field(min_length=1, max_length=MAX_INPUT_ARTIFACTS)]

    @model_validator(mode="after")
    def _validate_manifest(self):
        _require_pinned(self.linker_revision, "linker_revision")
        indices = [s.slot_index for s in self.slots]
        if indices != list(range(len(self.slots))):
            raise ValueError("context slots must have contiguous slot_index 0..n-1 in order")
        check_unique([s.evidence_id for s in self.slots], "context slot evidence_id")
        check_unique([r.relation_id for r in self.relations], "relation_id")
        check_unique([a.artifact_id for a in self.input_artifacts], "input_artifact artifact_id")
        return self


class VisualRegionRef(Contract):
    """Links a visual crop/render artifact to a specific evidence region."""

    evidence_id: NonWhiteId
    region_index: Annotated[int, Field(ge=0, le=MAX_REGIONS_PER_UNIT - 1)]


class VisualCropProvenance(Contract):
    """L0/L2 visual provenance bound into the cache key (SPEC 19.2, 19.3, 19.6)."""

    visual_object_id: NonWhiteId
    artifact_id: NonWhiteId
    image_sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=MAX_IMAGE_BYTES)]
    dpi: Annotated[int, Field(ge=72, le=600)]
    pixel_width: Annotated[int, Field(ge=1, le=4096)]
    pixel_height: Annotated[int, Field(ge=1, le=4096)]
    crop_bbox: tuple[
        Annotated[FiniteFloat, Field(ge=0.0, le=1.0)],
        Annotated[FiniteFloat, Field(ge=0.0, le=1.0)],
        Annotated[FiniteFloat, Field(ge=0.0, le=1.0)],
        Annotated[FiniteFloat, Field(ge=0.0, le=1.0)],
    ]
    rotation_degrees: Literal[0, 90, 180, 270]
    transform_id: NonWhiteId
    region_refs: Annotated[list[VisualRegionRef], Field(min_length=1, max_length=MAX_REGION_REFS)]

    @model_validator(mode="after")
    def _validate_visual(self):
        _require_pinned(self.transform_id, "transform_id")
        x0, y0, x1, y1 = self.crop_bbox
        if not (x0 < x1 and y0 < y1):
            raise ValueError("crop_bbox must satisfy x0 < x1 and y0 < y1")
        if self.pixel_width * self.pixel_height > 16_000_000:
            raise ValueError("visual crop exceeds 16 MP limit (SPEC 19.3)")
        check_unique(
            [(r.evidence_id, r.region_index) for r in self.region_refs],
            "visual region_ref",
        )
        return self


class GenerationConfig(Contract):
    """Model, prompt, schema and generation config versions (SPEC 6, 16.2, 16.6, 19.6)."""

    generator_config_version: NonWhiteId
    question_generator_model: NonWhiteId
    question_generator_prompt_version: NonWhiteId
    output_schema_version: NonWhiteId
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
    max_output_tokens: Annotated[int, Field(ge=1, le=1_000_000)]
    temperature: Annotated[FiniteFloat, Field(ge=0.0, le=2.0)] = 0.0
    seed: Optional[Annotated[int, Field(ge=0, le=2**63 - 1)]] = None
    vision_reader_model: Optional[NonWhiteId] = None
    vision_prompt_version: Optional[NonWhiteId] = None
    verifier_revision: Optional[NonWhiteId] = None
    selection_rule_version: Optional[NonWhiteId] = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("mode") == "json":
            raise TypeError(
                "GenerationConfig.model_dump(mode='json') is disabled because Pydantic converts "
                "exact Decimal parameter numbers to strings; use dump_generation_cache_input_json() "
                "or model_dump(mode='python')"
            )
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        """Serialize with lab-c14n-2 canonical_json so exact Decimal numbers in
        ``parameters`` remain JSON numbers (never converted to JSON strings)."""
        if args or kwargs:
            raise TypeError("GenerationConfig.model_dump_json() takes no formatting arguments; use canonical JSON")
        return canonical_json(_to_canonical_serializable(self.model_dump(mode="python", warnings=False))).decode("utf-8")

    @model_validator(mode="after")
    def _validate_config(self):
        for name in (
            "generator_config_version",
            "question_generator_model",
            "question_generator_prompt_version",
            "output_schema_version",
        ):
            _require_pinned(getattr(self, name), name)
        if (self.vision_reader_model is None) != (self.vision_prompt_version is None):
            raise ValueError("vision_reader_model and vision_prompt_version must both be set or both be null")
        if self.vision_reader_model is not None:
            _require_pinned(self.vision_reader_model, "vision_reader_model")
            _require_pinned(self.vision_prompt_version, "vision_prompt_version")
        if self.verifier_revision is not None:
            _require_pinned(self.verifier_revision, "verifier_revision")
        if self.selection_rule_version is not None:
            _require_pinned(self.selection_rule_version, "selection_rule_version")
        _check_json_value(self.parameters)
        overlap = set(self.parameters) & _RESERVED_PARAMETER_KEYS
        if overlap:
            raise ValueError(f"generation parameters must not shadow core config fields: {sorted(overlap)}")
        encoded = canonical_json(self.parameters)
        if len(encoded) > MAX_PARAMETER_BYTES:
            raise ValueError(f"generation parameters exceed {MAX_PARAMETER_BYTES} bytes")
        return self


class QuestionTargetSpec(Contract):
    """Question type, modalities and count requested from generation (SPEC 5.1, 6, 19.5)."""

    quota_bucket: QuotaBucket
    reasoning_type: ReasoningType
    modality_requirements: Annotated[list[Modality], Field(min_length=1, max_length=len(Modality))]
    question_count: Annotated[int, Field(ge=1, le=MAX_QUESTION_COUNT)]

    @model_validator(mode="after")
    def _validate_target(self):
        check_unique(self.modality_requirements, "modality_requirement")
        if self.quota_bucket is QuotaBucket.chart_table:
            if not (set(self.modality_requirements) & _VISUAL_MODALITIES):
                raise ValueError("chart_table target requires table or image modality")
        if self.reasoning_type in _VISUAL_REASONING_TYPES:
            if not (set(self.modality_requirements) & _VISUAL_MODALITIES):
                raise ValueError(f"{self.reasoning_type.value} requires table or image modality")
        return self


class GenerationCacheInput(Contract):
    """Complete input bound into a deterministic question generation cache key."""

    documents: Annotated[list[SourceDocumentScope], Field(min_length=1, max_length=MAX_DOCUMENTS)]
    parse_provenance: Annotated[list[ParseProvenance], Field(min_length=1, max_length=MAX_PARSE_MANIFESTS)]
    source_evidence: Annotated[list[EvidenceUnit], Field(min_length=1, max_length=MAX_EVIDENCE_UNITS)]
    context_manifest: ContextManifest
    generation_config: GenerationConfig
    target: QuestionTargetSpec
    visual_provenance: Annotated[list[VisualCropProvenance], Field(max_length=MAX_VISUAL_ARTIFACTS)] = Field(
        default_factory=list
    )

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("mode") == "json":
            raise TypeError(
                "GenerationCacheInput.model_dump(mode='json') is disabled because Pydantic converts "
                "exact Decimal parameter numbers to strings; use dump_generation_cache_input_json() "
                "or model_dump(mode='python')"
            )
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        """Serialize with lab-c14n-2 canonical_json so exact Decimal numbers in
        ``generation_config.parameters`` remain JSON numbers on round trip."""
        if args or kwargs:
            raise TypeError("GenerationCacheInput.model_dump_json() takes no formatting arguments; use canonical JSON")
        return dump_generation_cache_input_json(self).decode("utf-8")

    @model_validator(mode="after")
    def _validate_binding(self):
        check_unique([d.document_id for d in self.documents], "document_id")
        if len(self.documents) > 1:
            # SPEC 2 & 19.4: v1 benchmarks are built from a single PDF.
            raise ValueError("v1 generation cache requires a single source document")
        docs = {d.document_id: d for d in self.documents}

        check_unique([p.parse_run_id for p in self.parse_provenance], "parse_run_id")
        for p in self.parse_provenance:
            doc = docs.get(p.document_id)
            if doc is None:
                raise ValueError(f"parse_provenance[{p.parse_run_id}] references unknown document {p.document_id!r}")
            if any(page >= doc.page_count for page in p.page_selection):
                raise ValueError(f"parse_provenance[{p.parse_run_id}] selects page outside document page_count")

        check_unique([e.evidence_id for e in self.source_evidence], "evidence_id")
        evidence_by_id = {e.evidence_id: e for e in self.source_evidence}

        parsed_pages_by_doc: dict[str, set[int]] = {d_id: set() for d_id in docs}
        for p in self.parse_provenance:
            parsed_pages_by_doc[p.document_id].update(p.page_selection)

        total_content_bytes = 0
        for unit in self.source_evidence:
            _require_pinned(unit.extractor_revision, "extractor_revision")
            if len(unit.regions) > MAX_REGIONS_PER_UNIT:
                raise ValueError(f"evidence[{unit.evidence_id}] exceeds region limit")
            if len(unit.asset_refs) > MAX_ASSETS_PER_UNIT:
                raise ValueError(f"evidence[{unit.evidence_id}] exceeds asset_refs limit")
            check_unique(unit.asset_refs, "evidence asset_ref")
            check_unique(
                [(r.document_id, r.document_sha256, r.page_index, r.bbox, r.kind, r.quote) for r in unit.regions],
                "evidence region",
            )
            unit_bytes = len(unit.raw_content.encode("utf-8")) + len(unit.normalized_content.encode("utf-8"))
            if unit_bytes > MAX_UNIT_CONTENT_BYTES:
                raise ValueError(f"evidence[{unit.evidence_id}] content exceeds {MAX_UNIT_CONTENT_BYTES} bytes")
            total_content_bytes += unit_bytes
            for r in unit.regions:
                if r.quote is not None:
                    total_content_bytes += len(r.quote.encode("utf-8"))
                doc = docs.get(r.document_id)
                if doc is None:
                    raise ValueError(
                        f"evidence[{unit.evidence_id}] region references unknown document {r.document_id!r}"
                    )
                reasons = r.check_against_document(doc.document_sha256, doc.page_count)
                if reasons:
                    raise ValueError(f"evidence[{unit.evidence_id}] invalid region: {', '.join(reasons)}")
                if r.page_index not in parsed_pages_by_doc[r.document_id]:
                    raise ValueError(
                        f"evidence[{unit.evidence_id}] page {r.page_index} not covered by parse_provenance"
                    )
        if total_content_bytes > MAX_TOTAL_CONTENT_BYTES:
            raise ValueError("total source evidence content exceeds byte limit")

        # Validate context manifest slots against source_evidence
        slot_ids = {s.evidence_id for s in self.context_manifest.slots}
        if slot_ids != set(evidence_by_id):
            missing = sorted(set(evidence_by_id) - slot_ids)
            extra = sorted(slot_ids - set(evidence_by_id))
            raise ValueError(
                f"context_manifest slots must match source_evidence exactly (missing={missing}, extra={extra})"
            )

        for slot in self.context_manifest.slots:
            unit = evidence_by_id[slot.evidence_id]
            source_text = unit.raw_content if slot.content_mode == "raw" else unit.normalized_content
            if slot.truncated:
                assert slot.included_char_start is not None and slot.included_char_end is not None
                if slot.included_char_end > len(source_text):
                    raise ValueError(f"context slot {slot.evidence_id!r} char bounds exceed source content length")
                if slot.included_char_start == 0 and slot.included_char_end == len(source_text):
                    raise ValueError(
                        f"context slot {slot.evidence_id!r} marked truncated but slice covers full content"
                    )
                excerpt = source_text[slot.included_char_start : slot.included_char_end]
            else:
                if not source_text:
                    raise ValueError(f"context slot {slot.evidence_id!r} selects empty {slot.content_mode} content")
                if slot.included_char_start is not None and (
                    slot.included_char_start != 0 or slot.included_char_end != len(source_text)
                ):
                    raise ValueError(
                        f"untruncated context slot {slot.evidence_id!r} char bounds must be null or cover full content"
                    )
                excerpt = source_text
            actual_sha = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            if slot.included_content_sha256 != actual_sha:
                raise ValueError(f"context slot {slot.evidence_id!r} included_content_sha256 mismatch")

        # Validate context relations
        for rel in self.context_manifest.relations:
            if rel.from_evidence_id not in evidence_by_id or rel.to_evidence_id not in evidence_by_id:
                raise ValueError(f"relation {rel.relation_id!r} references evidence outside source_evidence")

        if (
            self.target.quota_bucket is QuotaBucket.cross_page
            or self.target.reasoning_type is ReasoningType.cross_page_integration
        ):
            pages = {page for u in self.source_evidence for page in u.pages()}
            if len(pages) < 2:
                raise ValueError("cross_page target requires source_evidence spanning at least 2 physical pages")

        # Validate visual provenance and asset mappings
        check_unique([v.visual_object_id for v in self.visual_provenance], "visual_object_id")
        check_unique([v.artifact_id for v in self.visual_provenance], "visual_provenance artifact_id")
        if sum(v.size_bytes for v in self.visual_provenance) > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("total visual provenance image size exceeds limit")

        covered_visual_regions: set[tuple[str, int]] = set()
        linked_assets_by_unit: dict[str, set[str]] = {eid: set() for eid in evidence_by_id}
        for vp in self.visual_provenance:
            for ref in vp.region_refs:
                unit = evidence_by_id.get(ref.evidence_id)
                if unit is None or ref.region_index >= len(unit.regions):
                    raise ValueError(
                        f"visual_provenance[{vp.visual_object_id}] references unknown evidence region"
                    )
                region = unit.regions[ref.region_index]
                if region.kind not in _VISUAL_REGION_KINDS:
                    raise ValueError(
                        f"visual_provenance[{vp.visual_object_id}] references non-visual region kind {region.kind.value}"
                    )
                if vp.artifact_id not in unit.asset_refs:
                    raise ValueError(
                        f"visual_provenance[{vp.visual_object_id}] artifact {vp.artifact_id!r} missing from "
                        f"evidence[{unit.evidence_id}].asset_refs"
                    )
                covered_visual_regions.add((ref.evidence_id, ref.region_index))
                linked_assets_by_unit[unit.evidence_id].add(vp.artifact_id)

        required_visual_regions = {
            (u.evidence_id, idx)
            for u in self.source_evidence
            for idx, r in enumerate(u.regions)
            if r.kind in _VISUAL_REGION_KINDS
        }
        if covered_visual_regions != required_visual_regions:
            raise ValueError("every visual source region in source_evidence requires visual_provenance coverage")
        for u in self.source_evidence:
            if set(u.asset_refs) != linked_assets_by_unit[u.evidence_id]:
                raise ValueError(
                    f"evidence[{u.evidence_id}].asset_refs must match visual_provenance artifacts exactly"
                )

        needs_visual = (
            bool(self.visual_provenance)
            or self.target.quota_bucket is QuotaBucket.chart_table
            or bool(set(self.target.modality_requirements) & _VISUAL_MODALITIES)
            or self.target.reasoning_type in _VISUAL_REASONING_TYPES
        )
        if needs_visual:
            if not self.visual_provenance:
                raise ValueError("visual target/modality requires visual_provenance and visual evidence")
            if (
                self.generation_config.vision_reader_model is None
                or self.generation_config.vision_prompt_version is None
            ):
                raise ValueError("visual generation requires vision_reader_model and vision_prompt_version")
        elif self.generation_config.vision_reader_model is not None:
            raise ValueError("vision_reader_model must be null when no visual evidence or modality is used")

        return self


@dataclass(frozen=True)
class GenerationCacheDescriptor:
    """Deterministic component digests and composite cache key (SPEC 6, 10.2, 19.6)."""

    key_version: str
    canonical_json_version: str
    cache_key: str
    source_set_sha256: str
    context_manifest_sha256: str
    generation_config_sha256: str
    target_sha256: str
    visual_provenance_sha256: Optional[str]
    canonical_payload: bytes = field(repr=False)


def _hash_canonical(obj: Any) -> tuple[str, bytes]:
    raw = canonical_json(obj)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("canonical cache snapshot exceeds 16 MiB")
    return hashlib.sha256(raw).hexdigest(), raw


def _canonical_region(r: SourceRegion) -> dict[str, Any]:
    return {
        "document_id": r.document_id,
        "document_sha256": r.document_sha256,
        "page_index": r.page_index,
        "bbox": list(r.bbox),
        "kind": r.kind.value,
        "quote": r.quote,
    }


def _region_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["document_id"],
        item["page_index"],
        item["bbox"],
        item["kind"],
        item["quote"] is None,
        item["quote"] or "",
    )


def _canonical_unit(unit: EvidenceUnit) -> dict[str, Any]:
    regions = sorted(
        (_canonical_region(r) for r in unit.regions),
        key=_region_sort_key,
    )
    return {
        "evidence_id": unit.evidence_id,
        "extractor_revision": unit.extractor_revision,
        "ocr_confidence": unit.ocr_confidence,
        "raw_content": unit.raw_content,
        "normalized_content": unit.normalized_content,
        "regions": regions,
        "asset_refs": sorted(unit.asset_refs),
    }


def _canonical_parse(p: ParseProvenance) -> dict[str, Any]:
    return {
        "parse_run_id": p.parse_run_id,
        "document_id": p.document_id,
        "parser_name": p.parser_name,
        "package_or_service_version": p.package_or_service_version,
        "adapter_revision": p.adapter_revision,
        "output_schema": p.output_schema,
        "model_revisions": [
            {"name": name, "revision": rev, "checksum": checksum}
            for name, rev, checksum in sorted(p.model_revisions, key=lambda item: item[0])
        ],
        "requested_profile": p.requested_profile,
        "effective_profile": p.effective_profile,
        "fallback_reason": p.fallback_reason,
        "ocr_languages": sorted(p.ocr_languages),
        "page_selection": sorted(p.page_selection),
        "render_dpi": p.render_dpi,
        "render_max_megapixels": p.render_max_megapixels,
        "postprocess_flags": sorted(p.postprocess_flags),
        "reproducible": p.reproducible,
    }


def build_generation_cache_key(cache_input: GenerationCacheInput) -> GenerationCacheDescriptor:
    """Validate and canonicalize ``cache_input`` into a deterministic cache key.

    Re-validates from Python data so ``model_construct`` or in-place mutation
    of nested lists/dicts cannot bypass boundary checks.

    Canonicalization rules:
    - Order-independent sets (documents, parse_provenance, source_evidence,
      regions within a unit, asset_refs, relations, input_artifacts,
      modality_requirements, visual_provenance) are sorted deterministically.
    - ``context_manifest.slots`` preserves its exact sequence (0..n-1) because
      prompt context order and per-slot truncation materially affect generation.
    - Display-only ``SourceRegion.page_label`` is excluded from the source-set
      hash (SPEC 4.1).
    """
    if not isinstance(cache_input, GenerationCacheInput):
        raise TypeError("cache_input must be a GenerationCacheInput")
    validated = GenerationCacheInput.model_validate(
        cache_input.model_dump(mode="python", warnings=False)
    )

    source_payload = {
        "documents": [
            {
                "document_id": d.document_id,
                "document_sha256": d.document_sha256,
                "page_count": d.page_count,
            }
            for d in sorted(validated.documents, key=lambda item: item.document_id)
        ],
        "parse_provenance": [
            _canonical_parse(p)
            for p in sorted(validated.parse_provenance, key=lambda item: item.parse_run_id)
        ],
        "evidence_units": [
            _canonical_unit(u)
            for u in sorted(validated.source_evidence, key=lambda item: item.evidence_id)
        ],
    }
    source_sha, _ = _hash_canonical(source_payload)

    cm = validated.context_manifest
    context_payload = {
        "manifest_id": cm.manifest_id,
        "linker_revision": cm.linker_revision,
        "slots": [
            {
                "slot_index": s.slot_index,
                "evidence_id": s.evidence_id,
                "content_mode": s.content_mode,
                "truncated": s.truncated,
                "included_char_start": s.included_char_start,
                "included_char_end": s.included_char_end,
                "included_content_sha256": s.included_content_sha256,
                "truncation_reason": s.truncation_reason,
            }
            for s in cm.slots
        ],
        "relations": [
            {
                "relation_id": r.relation_id,
                "relation_type": r.relation_type,
                "from_evidence_id": r.from_evidence_id,
                "to_evidence_id": r.to_evidence_id,
                "linker_revision": r.linker_revision,
                "validation_status": r.validation_status,
            }
            for r in sorted(cm.relations, key=lambda item: item.relation_id)
        ],
        "input_artifacts": [
            {
                "artifact_id": a.artifact_id,
                "sha256": a.sha256,
                "size_bytes": a.size_bytes,
                "mime_type": a.mime_type,
            }
            for a in sorted(cm.input_artifacts, key=lambda item: item.artifact_id)
        ],
    }
    context_sha, _ = _hash_canonical(context_payload)

    cfg = validated.generation_config
    config_payload = {
        "generator_config_version": cfg.generator_config_version,
        "question_generator_model": cfg.question_generator_model,
        "question_generator_prompt_version": cfg.question_generator_prompt_version,
        "output_schema_version": cfg.output_schema_version,
        "language": cfg.language,
        "max_output_tokens": cfg.max_output_tokens,
        "temperature": cfg.temperature,
        "seed": cfg.seed,
        "vision_reader_model": cfg.vision_reader_model,
        "vision_prompt_version": cfg.vision_prompt_version,
        "parameters": cfg.parameters,
    }
    config_sha, _ = _hash_canonical(config_payload)

    tgt = validated.target
    target_payload = {
        "quota_bucket": tgt.quota_bucket.value,
        "reasoning_type": tgt.reasoning_type.value,
        "modality_requirements": sorted(m.value for m in tgt.modality_requirements),
        "question_count": tgt.question_count,
    }
    target_sha, _ = _hash_canonical(target_payload)

    visual_sha: Optional[str] = None
    visual_payload: Optional[list[dict[str, Any]]] = None
    if validated.visual_provenance:
        evidence_by_id = {u.evidence_id: u for u in validated.source_evidence}
        canonical_region_index_map: dict[tuple[str, int], tuple[int, dict[str, Any]]] = {}
        for eid, u in evidence_by_id.items():
            indexed_regions = [(idx, _canonical_region(r)) for idx, r in enumerate(u.regions)]
            indexed_regions.sort(key=lambda pair: _region_sort_key(pair[1]))
            for canon_idx, (orig_idx, canon_reg) in enumerate(indexed_regions):
                canonical_region_index_map[(eid, orig_idx)] = (canon_idx, canon_reg)

        visual_payload = [
            {
                "visual_object_id": v.visual_object_id,
                "artifact_id": v.artifact_id,
                "image_sha256": v.image_sha256,
                "size_bytes": v.size_bytes,
                "dpi": v.dpi,
                "pixel_width": v.pixel_width,
                "pixel_height": v.pixel_height,
                "crop_bbox": list(v.crop_bbox),
                "rotation_degrees": v.rotation_degrees,
                "transform_id": v.transform_id,
                "region_refs": [
                    {
                        "evidence_id": r.evidence_id,
                        "canonical_region_index": canonical_region_index_map[(r.evidence_id, r.region_index)][0],
                        "region": canonical_region_index_map[(r.evidence_id, r.region_index)][1],
                    }
                    for r in sorted(
                        v.region_refs,
                        key=lambda item: (
                            item.evidence_id,
                            canonical_region_index_map[(item.evidence_id, item.region_index)][0],
                        ),
                    )
                ],
            }
            for v in sorted(validated.visual_provenance, key=lambda item: item.visual_object_id)
        ]
        visual_sha, _ = _hash_canonical(visual_payload)

    envelope = {
        "key_version": GENERATION_CACHE_KEY_VERSION,
        "canonical_json_version": CANONICAL_JSON_VERSION,
        "source_set_sha256": source_sha,
        "context_manifest_sha256": context_sha,
        "generation_config_sha256": config_sha,
        "target_sha256": target_sha,
        "visual_provenance_sha256": visual_sha,
        "components": {
            "source_set": source_payload,
            "context_manifest": context_payload,
            "generation_config": config_payload,
            "target": target_payload,
            "visual_provenance": visual_payload,
        },
    }
    cache_key, raw_bytes = _hash_canonical(envelope)
    return GenerationCacheDescriptor(
        key_version=GENERATION_CACHE_KEY_VERSION,
        canonical_json_version=CANONICAL_JSON_VERSION,
        cache_key=cache_key,
        source_set_sha256=source_sha,
        context_manifest_sha256=context_sha,
        generation_config_sha256=config_sha,
        target_sha256=target_sha,
        visual_provenance_sha256=visual_sha,
        canonical_payload=raw_bytes,
    )


def _to_canonical_serializable(value: Any) -> Any:
    """Convert validated Python contract structures into plain JSON-compatible
    containers while preserving exact ``Decimal`` instances for ``canonical_json``."""
    if value is None or isinstance(value, (bool, str, int, float, Decimal)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_to_canonical_serializable(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_canonical_serializable(v) for k, v in value.items()}
    raise TypeError(f"unsupported value for canonical serialization: {type(value).__name__}")


def dump_generation_cache_input_json(cache_input: GenerationCacheInput) -> bytes:
    """Serialize ``GenerationCacheInput`` to canonical UTF-8 JSON bytes
    (``lab-c14n-2``), preserving exact ``Decimal`` numeric tokens in
    ``generation_config.parameters`` as JSON numbers so round-tripping through
    ``parse_generation_cache_input`` yields an identical model and cache key."""
    if not isinstance(cache_input, GenerationCacheInput):
        raise TypeError("cache_input must be a GenerationCacheInput")
    validated = GenerationCacheInput.model_validate(
        cache_input.model_dump(mode="python", warnings=False)
    )
    serializable = _to_canonical_serializable(
        validated.model_dump(mode="python", warnings=False)
    )
    raw = canonical_json(serializable)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("generation cache input exceeds 16 MiB")
    return raw


def parse_generation_cache_input(raw: bytes | str) -> GenerationCacheInput:
    """Parse strict UTF-8 JSON into ``GenerationCacheInput`` (rejecting duplicate
    keys, NaN/Infinity and out-of-range numbers before Pydantic validation, and
    preserving exact Decimal numbers in ``generation_config.parameters`` while
    rejecting lossy binary64 rounding on float fields)."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("generation cache input exceeds 16 MiB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("invalid UTF-8 encoding") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("generation cache input exceeds 16 MiB")
        text = raw
    else:
        raise TypeError("raw input must be bytes or str")
    if text.startswith("\ufeff"):
        raise StrictJSONError("UTF-8 BOM is not permitted")
    raw_parsed = strict_loads(text)
    validated = GenerationCacheInput.model_validate_json(text)
    if isinstance(raw_parsed, dict):
        _check_lossless_numbers(raw_parsed, validated.model_dump(mode="python", warnings=False))
        raw_gen_cfg = raw_parsed.get("generation_config")
        if isinstance(raw_gen_cfg, dict) and "parameters" in raw_gen_cfg:
            py_data = validated.model_dump(mode="python", warnings=False)
            py_data["generation_config"]["parameters"] = raw_gen_cfg["parameters"]
            validated = GenerationCacheInput.model_validate(py_data)
    return validated
