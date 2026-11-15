"""Bounded, read-only visual evidence publication eligibility (SPEC 19.5/19.6).

The caller supplies authenticated, authorized verification records and an
artifact reader scoped to the same project. Records are attestations, never
proof of a human identity. This module neither creates reviews nor publishes
anything. See docs/VISUAL_VERIFICATION_GATE.md for the trust boundary.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Callable, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .base import Contract, Sha256, check_unique
from .benchmark import EvidenceUnit
from .source import RegionKind, SourceRegion

GATE_REVISION = "visual-publication-gate-1"
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
Id = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"\S")]
Ids = Annotated[list[Id], Field(min_length=1, max_length=128)]


class Risk(str, Enum):
    estimated_value = "estimated_value"
    cross_page_cell_merge = "cross_page_cell_merge"
    conflict = "conflict"
    dual_axis = "dual_axis"
    logarithmic_axis = "logarithmic_axis"
    image_text_conditions = "image_text_conditions"
    causal_claim = "causal_claim"


class ImageRegion(Contract):
    evidence_id: Id
    region_index: Annotated[int, Field(ge=0, le=127)]


class OriginalImage(Contract):
    """Trusted artifact manifest entry, not a URL or a caller-supplied path."""
    artifact_id: Id
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=MAX_IMAGE_BYTES)]
    region_refs: Annotated[list[ImageRegion], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def _unique(self):
        check_unique([(r.evidence_id, r.region_index) for r in self.region_refs], "image region ref")
        return self


class DocumentScope(Contract):
    document_id: Id
    sha256: Sha256
    page_count: Annotated[int, Field(gt=0, le=2000)]


class VisualSubject(Contract):
    """One immutable revision. Risk classification is trusted upstream input."""
    subject_id: Id
    revision: Id
    producer_operation_ids: Ids
    documents: Annotated[list[DocumentScope], Field(min_length=1, max_length=128)]
    evidence: Annotated[list[EvidenceUnit], Field(min_length=1, max_length=128)]
    images: Annotated[list[OriginalImage], Field(min_length=1, max_length=128)]
    risks: Annotated[list[Risk], Field(max_length=len(Risk))]
    unresolved: bool

    @model_validator(mode="after")
    def _references(self):
        check_unique(self.producer_operation_ids, "producer operation ID")
        check_unique(self.risks, "risk")
        check_unique([e.evidence_id for e in self.evidence], "evidence ID")
        check_unique([a.artifact_id for a in self.images], "image ID")
        check_unique([d.document_id for d in self.documents], "document ID")
        documents = {d.document_id: d for d in self.documents}
        evidence = {e.evidence_id: e for e in self.evidence}
        if sum(a.size_bytes for a in self.images) > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("total images exceed byte limit")
        covered = set()
        linked_images = {eid: set() for eid in evidence}
        for image in self.images:
            for ref in image.region_refs:
                item = evidence.get(ref.evidence_id)
                if (item is None or image.artifact_id not in item.asset_refs
                        or ref.region_index >= len(item.regions)
                        or item.regions[ref.region_index].kind not in
                        {RegionKind.table, RegionKind.figure, RegionKind.image, RegionKind.page}):
                    raise ValueError("image must reference an existing visual evidence region/asset")
                covered.add((ref.evidence_id, ref.region_index))
                linked_images[ref.evidence_id].add(image.artifact_id)
        required = {(e.evidence_id, i) for e in self.evidence for i, r in enumerate(e.regions)
                    if r.kind in {RegionKind.table, RegionKind.figure, RegionKind.image, RegionKind.page}}
        if covered != required:
            raise ValueError("every visual region requires an original image")
        if sum(len(e.raw_content.encode()) + len(e.normalized_content.encode())
               + sum(len(r.quote.encode()) if r.quote is not None else 0 for r in e.regions)
               for e in self.evidence) > 8 * 1024 * 1024:
            raise ValueError("total evidence content limit exceeded")
        for item in self.evidence:
            if set(item.asset_refs) != linked_images[item.evidence_id]:
                raise ValueError("every evidence asset ref must have an original image mapping")
            for region in item.regions:
                document = documents.get(region.document_id)
                if document is None or region.check_against_document(document.sha256, document.page_count):
                    raise ValueError("source region outside document scope")
            if len(item.regions) > 128 or len(item.asset_refs) > 128:
                raise ValueError("evidence reference limit exceeded")
            if len(item.raw_content.encode()) + len(item.normalized_content.encode()) > 1024 * 1024:
                raise ValueError("evidence content limit exceeded")
            if not any(r.kind in {RegionKind.table, RegionKind.figure, RegionKind.image, RegionKind.page}
                       for r in item.regions):
                raise ValueError("visual evidence requires a visual source region")
        return self


class Verification(Contract):
    """Record resolved from trusted storage, bound to exactly one snapshot."""
    verification_id: Id
    subject_sha256: Sha256
    operation_id: Id
    oracle_revision: Id
    oracle: Literal["human", "automated"]
    verdict: Literal["pass", "fail", "needs_review"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=8000, pattern=r"\S")]
    checked_evidence_ids: Ids
    checked_image_refs: Ids
    checked_risks: Annotated[list[Risk], Field(max_length=len(Risk))]
    additional_risks: Annotated[list[Risk], Field(max_length=len(Risk))]
    reviewer_id: Id | None
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def _references(self):
        check_unique(self.checked_evidence_ids, "checked evidence ID")
        check_unique(self.checked_image_refs, "checked image ref")
        check_unique(self.checked_risks, "checked risk")
        check_unique(self.additional_risks, "additional risk")
        if (self.oracle == "human") != (self.reviewer_id is not None):
            raise ValueError("only human verification requires a reviewer identity")
        return self


@dataclass(frozen=True)
class GateProblem:
    code: str
    reference: str


@dataclass(frozen=True)
class GateResult:
    gate_revision: str
    subject_sha256: str
    eligible: bool
    problems: tuple[GateProblem, ...]
    verification_ids: tuple[str, ...]


def _ids(values, maximum=128) -> None:
    if type(values) is not list or len(values) > maximum:
        raise ValueError("reference cardinality limit exceeded")
    if any(not isinstance(v, str) or len(v) > 128 for v in values):
        raise ValueError("reference size limit exceeded")


def _scalar_strings(model, limits) -> None:
    for name, limit in limits.items():
        value = getattr(model, name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError("scalar string size/type limit exceeded")


def _integer(value, maximum) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("bounded integer required")


def _preflight_verification(record: Verification) -> None:
    if not isinstance(record, Verification):
        raise ValueError("verification record required")
    if (not isinstance(record.reviewed_at, datetime)
            or record.reviewed_at.tzinfo is None or record.reviewed_at.utcoffset() is None):
        raise ValueError("review timestamp required")
    _ids(record.checked_evidence_ids)
    _ids(record.checked_image_refs)
    _ids(record.checked_risks, len(Risk))
    _ids(record.additional_risks, len(Risk))
    _scalar_strings(record, dict(verification_id=128, subject_sha256=64, operation_id=128,
                                oracle_revision=128, oracle=16, verdict=16, reason=8000,
                                reviewer_id=128))


def _preflight(subject: VisualSubject) -> None:
    # Bound recursive serialization even for Python model_construct callers.
    if not isinstance(subject, VisualSubject):
        raise ValueError("visual subject required")
    for values in [subject.evidence, subject.images, subject.documents,
                   subject.producer_operation_ids, subject.risks]:
        if type(values) is not list or len(values) > 128:
            raise ValueError("subject cardinality limit exceeded")
    if type(subject.unresolved) is not bool:
        raise ValueError("strict unresolved boolean required")
    _ids(subject.producer_operation_ids)
    _ids(subject.risks, len(Risk))
    _scalar_strings(subject, dict(subject_id=128, revision=128))
    for document in subject.documents:
        if not isinstance(document, DocumentScope):
            raise ValueError("document scope required")
        _scalar_strings(document, dict(document_id=128, sha256=64))
        _integer(document.page_count, 2000)
    for image in subject.images:
        if not isinstance(image, OriginalImage):
            raise ValueError("image manifest required")
        _scalar_strings(image, dict(artifact_id=128, sha256=64))
        _integer(image.size_bytes, MAX_IMAGE_BYTES)
        if type(image.region_refs) is not list or len(image.region_refs) > 128:
            raise ValueError("image region reference limit exceeded")
        for ref in image.region_refs:
            if not isinstance(ref, ImageRegion):
                raise ValueError("image region reference required")
            _scalar_strings(ref, dict(evidence_id=128))
            _integer(ref.region_index, 127)
    total = 0
    for item in subject.evidence:
        if not isinstance(item, EvidenceUnit):
            raise ValueError("evidence unit required")
        if type(item.regions) is not list or len(item.regions) > 128:
            raise ValueError("region limit exceeded")
        if type(item.asset_refs) is not list or len(item.asset_refs) > 128:
            raise ValueError("asset reference limit exceeded")
        _ids(item.asset_refs)
        _scalar_strings(item, dict(evidence_id=128, extractor_revision=128))
        if item.ocr_confidence is not None and (type(item.ocr_confidence) not in {int, float}
                                               or not 0 <= item.ocr_confidence <= 1):
            raise ValueError("bounded OCR confidence required")
        for region in item.regions:
            if not isinstance(region, SourceRegion):
                raise ValueError("source region required")
            _scalar_strings(region, dict(document_id=128, document_sha256=64, page_label=64, kind=16))
            if not isinstance(region.kind, RegionKind):
                raise ValueError("source region kind required")
            _integer(region.page_index, 1999)
            if (type(region.bbox) is not tuple or len(region.bbox) != 4
                    or any(type(c) not in {int, float} or not 0 <= c <= 1 for c in region.bbox)):
                raise ValueError("bounded numeric bbox required")
        values = [item.raw_content, item.normalized_content]
        values.extend(r.quote for r in item.regions if r.quote is not None)
        for value in values:
            if type(value) is not str or len(value) > 1024 * 1024:
                raise ValueError("evidence content limit exceeded")
            total += len(value.encode())
            if total > 8 * 1024 * 1024:
                raise ValueError("total evidence content limit exceeded")


def _snapshot(subject: VisualSubject) -> tuple[VisualSubject, str]:
    # Revalidate Python callers too: model_construct / model_copy must not
    # bypass the boundary. Copy nested lists so the gate never mutates input.
    _preflight(subject)
    validated = VisualSubject.model_validate(subject.model_dump(mode="python", warnings=False))
    payload = json.dumps({"gate_revision": GATE_REVISION,
                          "subject": validated.model_dump(mode="json")},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode()
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("subject snapshot exceeds 16 MiB")
    return validated, hashlib.sha256(payload).hexdigest()


def subject_hash(subject: VisualSubject) -> str:
    """Hash normalized typed snapshot; not a replacement for raw artifact hashes."""
    return _snapshot(subject)[1]


def check_visual_publication(
    subject: VisualSubject,
    trusted_verifications: list[Verification],
    read_image: Callable[[str, int], bytes],
    authorize_verification: Callable[[Verification], bool],
) -> GateResult:
    """Return eligibility, never alter verified/frozen state.

    ``read_image(id, maximum_bytes)`` must return committed original image
    bytes using an authorized, bounded artifact read, or raise. It must not
    resolve arbitrary URLs. All current-snapshot records must be supplied;
    negative records block that revision even when another reviewer passes.
    ``authorize_verification`` must authenticate record provenance and authorize
    its oracle/reviewer role in this project. It receives a defensive copy.
    Loader failure details are deliberately omitted from returned diagnostics.
    """
    subject, digest = _snapshot(subject)
    if type(trusted_verifications) is not list or len(trusted_verifications) > 128:
        raise ValueError("verification list limit exceeded")
    for record in trusted_verifications:
        _preflight_verification(record)
    records = [Verification.model_validate(v.model_dump(mode="python", warnings=False)) for v in trusted_verifications]
    check_unique([v.verification_id for v in records], "verification ID")
    check_unique([v.operation_id for v in records], "verification operation ID")
    problems: list[GateProblem] = []
    if subject.unresolved:
        problems.append(GateProblem("unresolved_evidence", subject.subject_id))
    for image in subject.images:
        try:
            data = read_image(image.artifact_id, image.size_bytes + 1)
        except Exception:
            problems.append(GateProblem("image_unavailable", image.artifact_id))
            continue
        if type(data) is not bytes or len(data) != image.size_bytes:
            problems.append(GateProblem("image_size_mismatch", image.artifact_id))
        elif hashlib.sha256(data).hexdigest() != image.sha256:
            problems.append(GateProblem("image_hash_mismatch", image.artifact_id))
    evidence_ids = {e.evidence_id for e in subject.evidence}
    image_ids = {a.artifact_id for a in subject.images}
    producers = set(subject.producer_operation_ids)
    risks = set(subject.risks)
    accepted: list[str] = []
    human = False
    for record in records:
        ref = record.verification_id
        try:
            authorized = authorize_verification(record.model_copy(deep=True)) is True
        except Exception:
            authorized = False
        if not authorized:
            problems.append(GateProblem("verification_unauthorized", ref))
            continue
        if record.subject_sha256 != digest:
            problems.append(GateProblem("verification_snapshot_mismatch", ref))
            continue
        if record.operation_id in producers:
            problems.append(GateProblem("verifier_not_independent", ref))
            continue
        if (set(record.checked_evidence_ids) != evidence_ids
                or set(record.checked_image_refs) != image_ids
                or set(record.checked_risks) != risks):
            problems.append(GateProblem("verification_scope_mismatch", ref))
            continue
        if set(record.additional_risks) - risks:
            problems.append(GateProblem("risk_revision_required", ref))
            continue
        if record.verdict != "pass":
            problems.append(GateProblem("verification_" + record.verdict, ref))
            continue
        accepted.append(ref)
        human |= record.oracle == "human"
    if not accepted:
        problems.append(GateProblem("independent_verification_required", subject.subject_id))
    if risks and not human:
        problems.append(GateProblem("human_verification_required", subject.subject_id))
    return GateResult(GATE_REVISION, digest, not problems, tuple(problems), tuple(accepted))
