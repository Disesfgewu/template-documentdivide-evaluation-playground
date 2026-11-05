"""Four-part benchmark contracts (SPEC 4.1, 5.1-5.2, 16.2, 19.5).

The four parts (questions, semantic_answers, textual_answers, source_answers)
are separate records joined only by ``question_id`` -- never by array
position. Per-record rules live on the models; rules that need several parts
at once (foreign keys, cross-page validity, quotas) are in ``validate_bundle``
and ``check_manifest``, which return located problems instead of raising, so
a caller can report every problem with its field path (T12/T13).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import AwareDatetime, Discriminator, Field, StringConstraints, Tag, model_validator

from .base import Contract, FiniteFloat, ResourceId, Sha256
from .base import check_unique as _unique
from .method import Modality
from .source import RegionKind, SourceRegion

NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
Weight = Annotated[FiniteFloat, Field(gt=0)]
Unit01 = Annotated[FiniteFloat, Field(ge=0.0, le=1.0)]
NonNegFloat = Annotated[FiniteFloat, Field(ge=0.0)]
# BCP 47-like tag, e.g. "zh-Hant", "en", "ja-JP".
Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
IdList = Annotated[list[ResourceId], Field(min_length=1)]


# --- 5.1 targets -----------------------------------------------------------------

def target_counts(page_count: int) -> tuple[int, int]:
    """N = max(20, P) and Q = ceil(0.10 * N), in exact integer arithmetic."""
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise ValueError("page_count must be an integer >= 1")
    n = max(20, page_count)
    return n, -(-n // 10)


class QuotaBucket(str, Enum):
    general = "general"
    cross_page = "cross_page"
    chart_table = "chart_table"
    confusable = "confusable"


SPECIAL_BUCKETS = (QuotaBucket.cross_page, QuotaBucket.chart_table, QuotaBucket.confusable)


class ReasoningType(str, Enum):
    value_lookup = "value_lookup"
    trend = "trend"
    comparison = "comparison"
    calculation = "calculation"
    diagram_relation = "diagram_relation"
    image_text_integration = "image_text_integration"
    cross_page_integration = "cross_page_integration"


# --- 4.1 evidence ----------------------------------------------------------------

class EvidenceUnit(Contract):
    evidence_id: ResourceId
    regions: Annotated[list[SourceRegion], Field(min_length=1)]
    raw_content: str            # as extracted; kept separately from normalized
    normalized_content: str
    extractor_revision: ResourceId
    ocr_confidence: Optional[Unit01]  # null = unknown / not OCR
    asset_refs: list[ResourceId]      # table/image assets; [] if none

    def pages(self) -> set[tuple[str, int]]:
        return {(r.document_id, r.page_index) for r in self.regions}


# --- questions -------------------------------------------------------------------

class Question(Contract):
    question_id: ResourceId
    ordinal: Annotated[int, Field(ge=0)]
    question_text: NonEmpty
    language: Language
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]]
    quota_bucket: QuotaBucket
    question_hash: Sha256
    modality_requirements: Annotated[list[Modality], Field(min_length=1)]
    reasoning_type: ReasoningType
    visual_object_refs: list[ResourceId]

    @model_validator(mode="after")
    def _distinct(self):
        _unique(self.tags, "tag")
        _unique(self.modality_requirements, "modality")
        _unique(self.visual_object_refs, "visual_object_ref")
        return self


# --- semantic answers ------------------------------------------------------------

class ScoringRule(Contract):
    """Defined at authoring time. partial_credit null = no 0.5 score allowed."""

    full_credit: NonEmpty
    partial_credit: Optional[NonEmpty]
    no_credit: NonEmpty


class Fact(Contract):
    fact_id: ResourceId
    statement: NonEmpty
    weight: Weight
    allowed_equivalences: list[NonEmpty]
    scoring_rule: ScoringRule


class Operand(Contract):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    value: FiniteFloat
    unit: Optional[Annotated[str, StringConstraints(min_length=1, max_length=32)]]
    evidence_id: ResourceId


class Ref(Contract):
    ref: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class Const(Contract):
    const: FiniteFloat


class Op(Contract):
    """Restricted formula AST: never executable model-generated code."""

    op: Literal["add", "sub", "mul", "div", "neg", "abs", "min", "max"]
    args: Annotated[list["Expr"], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def _arity(self):
        # Checked on every node, so any Op (not only one inside Calculation)
        # is bounded. Children are already validated, so their depth is known.
        if 1 + max((_depth(a) for a in self.args), default=0) > MAX_EXPR_DEPTH:
            raise ValueError(f"formula deeper than {MAX_EXPR_DEPTH}")
        n = len(self.args)
        if self.op in ("sub", "div") and n != 2:
            raise ValueError(f"{self.op} takes exactly 2 args")
        if self.op in ("neg", "abs") and n != 1:
            raise ValueError(f"{self.op} takes exactly 1 arg")
        if self.op in ("add", "mul", "min", "max") and n < 2:
            raise ValueError(f"{self.op} takes at least 2 args")
        return self


MAX_EXPR_DEPTH = 16


def _depth(e) -> int:
    return 1 + max((_depth(a) for a in e.args), default=0) if isinstance(e, Op) else 1


def _expr_tag(v) -> Optional[str]:
    # Tag by the node's key so a bad node yields one precise error, not one
    # per union member.
    if isinstance(v, dict):
        for tag in ("op", "ref", "const"):
            if tag in v:
                return tag
        return None
    return {Op: "op", Ref: "ref", Const: "const"}.get(type(v))


Expr = Annotated[Union[Annotated[Op, Tag("op")], Annotated[Ref, Tag("ref")], Annotated[Const, Tag("const")]],
                 Discriminator(_expr_tag)]
Op.model_rebuild()


def _refs(e: Expr) -> set[str]:
    if isinstance(e, Ref):
        return {e.ref}
    if isinstance(e, Const):
        return set()
    out: set[str] = set()
    for a in e.args:
        out |= _refs(a)
    return out


class Calculation(Contract):
    operands: Annotated[list[Operand], Field(min_length=1)]
    formula: Expr
    answer_unit: Optional[Annotated[str, StringConstraints(min_length=1, max_length=32)]]

    @model_validator(mode="after")
    def _refs_resolve(self):
        names = [o.name for o in self.operands]
        _unique(names, "operand name")
        used = _refs(self.formula)
        missing = used - set(names)
        if missing:
            raise ValueError(f"formula references unknown operands: {sorted(missing)}")
        unused = set(names) - used
        if unused:
            # An unused operand would attach unchecked evidence to the answer.
            raise ValueError(f"operands not used by the formula: {sorted(unused)}")
        return self


class SemanticAnswer(Contract):
    question_id: ResourceId
    reference_answer: NonEmpty
    required_facts: Annotated[list[Fact], Field(min_length=1)]
    conditions: list[NonEmpty]
    calculation: Optional[Calculation]

    @model_validator(mode="after")
    def _distinct(self):
        _unique([f.fact_id for f in self.required_facts], "fact_id")
        return self


# --- textual answers -------------------------------------------------------------

class TextCheck(Contract):
    """kind=quote/entity: expected is the string; numeric fields are null.
    kind=numeric: exactly one of expected (number, with tolerances) or
    interval [low, high] (frozen; tolerances must be null so the interval is
    never widened twice)."""

    check_id: ResourceId
    fact_id: ResourceId
    kind: Literal["quote", "entity", "numeric"]
    expected: Optional[Union[str, FiniteFloat]]
    interval: Optional[tuple[FiniteFloat, FiniteFloat]]
    unit: Optional[Annotated[str, StringConstraints(min_length=1, max_length=32)]]
    normalization_rule: Optional[ResourceId]
    absolute_tolerance: Optional[NonNegFloat]
    relative_tolerance: Optional[NonNegFloat]
    precision: Optional[Annotated[int, Field(ge=0, le=12)]]
    supporting_evidence_ids: IdList

    @model_validator(mode="after")
    def _shape(self):
        numeric_fields = (self.interval, self.absolute_tolerance, self.relative_tolerance, self.precision)
        if self.kind in ("quote", "entity"):
            if not isinstance(self.expected, str) or not self.expected:
                raise ValueError(f"{self.kind} check requires a non-empty string expected")
            if any(v is not None for v in numeric_fields) or self.unit is not None:
                raise ValueError(f"{self.kind} check must leave numeric fields and unit null")
        else:
            has_point = self.expected is not None
            if has_point == (self.interval is not None):
                raise ValueError("numeric check requires exactly one of expected or interval")
            if has_point and isinstance(self.expected, str):
                raise ValueError("numeric expected must be a number")
            if self.interval is not None:
                lo, hi = self.interval
                if not lo <= hi:
                    raise ValueError("interval requires low <= high")
                if any(v is not None for v in (self.absolute_tolerance, self.relative_tolerance, self.precision)):
                    raise ValueError("interval checks must not also carry tolerances or precision")
            elif self.absolute_tolerance is None and self.relative_tolerance is None:
                raise ValueError("point numeric check requires absolute_tolerance or relative_tolerance")
            elif self.expected == 0 and self.absolute_tolerance is None:
                # rel_tol * |0| == 0: a relative tolerance alone would demand exact zero.
                raise ValueError("expected 0 requires absolute_tolerance (relative tolerance vanishes at zero)")
        _unique(self.supporting_evidence_ids, "supporting_evidence_id")
        return self

    def numeric_matches(self, actual: float) -> bool:
        """SPEC 19.5, after unit conversion by the caller: point check
        abs(actual-expected) <= max(abs_tol, rel_tol*abs(expected)); interval
        check low <= actual <= high with no further widening."""
        if self.kind != "numeric":
            raise ValueError("numeric_matches applies to numeric checks only")
        if not math.isfinite(actual):
            return False
        if self.interval is not None:
            lo, hi = self.interval
            return lo <= actual <= hi
        tol = max(self.absolute_tolerance or 0.0, (self.relative_tolerance or 0.0) * abs(self.expected))
        return abs(actual - self.expected) <= tol


class TextualAnswer(Contract):
    question_id: ResourceId
    checks: list[TextCheck]  # [] when no source string applies (not a made-up OCR text)

    @model_validator(mode="after")
    def _distinct(self):
        _unique([c.check_id for c in self.checks], "check_id")
        return self


# --- source answers --------------------------------------------------------------

class EvidenceSet(Contract):
    set_id: ResourceId
    evidence_ids: IdList

    @model_validator(mode="after")
    def _distinct(self):
        _unique(self.evidence_ids, "evidence_id")
        return self


class FactSupportSet(Contract):
    fact_id: ResourceId
    support_sets: Annotated[list[IdList], Field(min_length=1)]  # OR-of-AND

    @model_validator(mode="after")
    def _distinct(self):
        for s in self.support_sets:
            _unique(s, "evidence_id")
        return self


class Distractor(Contract):
    evidence_id: ResourceId
    confusion_reason: NonEmpty
    disambiguating_condition: NonEmpty


class SourceAnswer(Contract):
    question_id: ResourceId
    evidence_sets: Annotated[list[EvidenceSet], Field(min_length=1)]
    fact_support_sets: list[FactSupportSet]
    distractors: list[Distractor]
    validation_refs: list[ResourceId]

    @model_validator(mode="after")
    def _distinct(self):
        _unique([s.set_id for s in self.evidence_sets], "set_id")
        _unique([f.fact_id for f in self.fact_support_sets], "fact_support fact_id")
        _unique([d.evidence_id for d in self.distractors], "distractor evidence_id")
        return self


class SourceCheck(Contract):
    evidence_id: ResourceId
    check: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    passed: bool


class ValidationRecord(Contract):
    validation_id: ResourceId
    question_id: ResourceId
    validator_revision: ResourceId
    operation_id: ResourceId
    verdict: Literal["pass", "fail", "needs_review"]
    reason: NonEmpty
    source_checks: list[SourceCheck]
    confidence: Optional[Unit01]  # null when the validator gives none
    reviewer: Optional[ResourceId]  # human reviewer; null for automated only
    reviewed_at: Optional[AwareDatetime]

    @model_validator(mode="after")
    def _review_pair(self):
        if (self.reviewer is None) != (self.reviewed_at is None):
            raise ValueError("reviewer and reviewed_at must both be set or both be null")
        return self


# --- cross-part validation -------------------------------------------------------

@dataclass(frozen=True)
class Problem:
    path: str   # e.g. "source_answers[q_7].evidence_sets[s1].evidence_ids[e9]"
    code: str   # stable machine code
    detail: str


def _index(records, part: str, problems: list[Problem]) -> dict:
    out = {}
    for i, r in enumerate(records):
        if r.question_id in out:
            problems.append(Problem(f"{part}[{i}]", "duplicate_question_id", r.question_id))
        else:
            out[r.question_id] = r
    return out


def validate_bundle(questions: list[Question], semantic: list[SemanticAnswer],
                    textual: list[TextualAnswer], source: list[SourceAnswer],
                    evidence: dict[str, EvidenceUnit]) -> list[Problem]:
    """Join the four parts by question_id and check every cross reference.
    Input order is irrelevant. Empty list = consistent."""
    p: list[Problem] = []
    docs: set[tuple[str, str]] = set()
    for key, unit in evidence.items():
        if key != unit.evidence_id:
            p.append(Problem(f"evidence[{key}]", "evidence_key_mismatch", unit.evidence_id))
        docs |= {(r.document_id, r.document_sha256) for r in unit.regions}
    if len(docs) > 1:
        # v1 benchmarks are built from one PDF (SPEC 2, 19.4); pages of
        # different documents must never count as "two pages".
        p.append(Problem("evidence", "multiple_documents", f"{len(docs)} (document_id, sha256) pairs"))
    qs = _index(questions, "questions", p)
    parts = {"semantic_answers": _index(semantic, "semantic_answers", p),
             "textual_answers": _index(textual, "textual_answers", p),
             "source_answers": _index(source, "source_answers", p)}

    ordinals = Counter(q.ordinal for q in qs.values())
    for qid, q in qs.items():
        if ordinals[q.ordinal] > 1:
            p.append(Problem(f"questions[{qid}].ordinal", "duplicate_ordinal", str(q.ordinal)))
    hashes = Counter(q.question_hash for q in qs.values())
    for qid, q in qs.items():
        if hashes[q.question_hash] > 1:
            p.append(Problem(f"questions[{qid}].question_hash", "duplicate_question", q.question_hash))

    for part, idx in parts.items():
        for qid in sorted(set(qs) - set(idx)):
            p.append(Problem(f"{part}[{qid}]", "missing_record", "question has no record in this part"))
        for qid in sorted(set(idx) - set(qs)):
            p.append(Problem(f"{part}[{qid}]", "unknown_question_id", "no such question"))

    def ev(path: str, eid: str):
        if eid not in evidence:
            p.append(Problem(path, "unknown_evidence_id", eid))

    for qid in sorted(qs):
        q = qs[qid]
        sem = parts["semantic_answers"].get(qid)
        txt = parts["textual_answers"].get(qid)
        src = parts["source_answers"].get(qid)
        facts = {f.fact_id for f in sem.required_facts} if sem else set()

        if sem and sem.calculation:
            for o in sem.calculation.operands:
                ev(f"semantic_answers[{qid}].calculation.operands[{o.name}].evidence_id", o.evidence_id)
        if txt:
            for c in txt.checks:
                base = f"textual_answers[{qid}].checks[{c.check_id}]"
                if sem and c.fact_id not in facts:
                    p.append(Problem(f"{base}.fact_id", "unknown_fact_id", c.fact_id))
                for e in c.supporting_evidence_ids:
                    ev(f"{base}.supporting_evidence_ids[{e}]", e)
        if not src:
            continue
        base = f"source_answers[{qid}]"
        for s in src.evidence_sets:
            for e in s.evidence_ids:
                ev(f"{base}.evidence_sets[{s.set_id}].evidence_ids[{e}]", e)
        for fs in src.fact_support_sets:
            if sem and fs.fact_id not in facts:
                p.append(Problem(f"{base}.fact_support_sets[{fs.fact_id}]", "unknown_fact_id", fs.fact_id))
            for alt in fs.support_sets:
                for e in alt:
                    ev(f"{base}.fact_support_sets[{fs.fact_id}].support_sets[{e}]", e)
        if sem:
            covered = {fs.fact_id for fs in src.fact_support_sets}
            for f in sorted(facts - covered):
                p.append(Problem(f"{base}.fact_support_sets[{f}]", "fact_without_support", f))
        # Every acceptable evidence set must carry, for each required fact, at
        # least one of that fact's support alternatives (SPEC 16.2: a set
        # "整組能支持所有必要事實").
        support = {fs.fact_id: fs.support_sets for fs in src.fact_support_sets}
        for s in src.evidence_sets:
            for f in sorted(facts & set(support)):
                if not any(set(alt) <= set(s.evidence_ids) for alt in support[f]):
                    p.append(Problem(f"{base}.evidence_sets[{s.set_id}]", "set_misses_fact_support", f))

        gold = {e for s in src.evidence_sets for e in s.evidence_ids}
        gold |= {e for alts in support.values() for alt in alts for e in alt}
        if txt:
            gold |= {e for c in txt.checks for e in c.supporting_evidence_ids}
        if sem and sem.calculation:
            gold |= {o.evidence_id for o in sem.calculation.operands}
        for d in src.distractors:
            ev(f"{base}.distractors[{d.evidence_id}]", d.evidence_id)
            if d.evidence_id in gold:
                p.append(Problem(f"{base}.distractors[{d.evidence_id}]", "distractor_is_gold", d.evidence_id))

        if q.quota_bucket is QuotaBucket.confusable and not src.distractors:
            p.append(Problem(f"{base}.distractors", "confusable_without_distractor", qid))
        if q.quota_bucket is QuotaBucket.chart_table:
            # Contract-level floor only; the removal/necessity test is #10.
            visual = {RegionKind.table, RegionKind.figure, RegionKind.image}
            for s in src.evidence_sets:
                kinds = {r.kind for e in s.evidence_ids if e in evidence for r in evidence[e].regions}
                if not kinds & visual:
                    p.append(Problem(f"{base}.evidence_sets[{s.set_id}]", "chart_table_without_visual_evidence", qid))
        if q.quota_bucket is QuotaBucket.cross_page and sem and set(support) >= facts:
            # A single page that supports every fact on its own also disqualifies.
            def one_page_options(alts):
                out = set()
                for alt in alts:
                    if all(e in evidence for e in alt):
                        pages = set().union(*(evidence[e].pages() for e in alt))
                        if len(pages) == 1:
                            out |= pages
                return out
            common = set.intersection(*(one_page_options(support[f]) for f in facts)) if facts else set()
            for doc, page in sorted(common):
                p.append(Problem(f"{base}.fact_support_sets", "cross_page_single_page", f"page {page}"))
        if q.quota_bucket is QuotaBucket.cross_page:
            # Every acceptable set must need >= 2 physical pages; one single-page
            # alternative disqualifies the question from the cross_page bucket.
            for s in src.evidence_sets:
                if any(e not in evidence for e in s.evidence_ids):
                    continue
                pages = set().union(*(evidence[e].pages() for e in s.evidence_ids))
                if len(pages) < 2:
                    p.append(Problem(f"{base}.evidence_sets[{s.set_id}]", "cross_page_single_page", qid))
    return p


# --- manifest --------------------------------------------------------------------

class PartHashes(Contract):
    questions: Sha256
    semantic_answers: Sha256
    textual_answers: Sha256
    source_answers: Sha256


class QuotaCounts(Contract):
    general: Annotated[int, Field(ge=0)]
    cross_page: Annotated[int, Field(ge=0)]
    chart_table: Annotated[int, Field(ge=0)]
    confusable: Annotated[int, Field(ge=0)]


class BenchmarkManifest(Contract):
    schema_version: Literal["v1"]
    benchmark_id: ResourceId
    revision_id: ResourceId
    document_id: ResourceId
    document_sha256: Sha256
    page_count: Annotated[int, Field(ge=1)]
    file_hashes: PartHashes
    n_target: Annotated[int, Field(ge=20)]
    n_valid: Annotated[int, Field(ge=0)]
    q_min: Annotated[int, Field(ge=2)]
    quota_counts: QuotaCounts
    compliance: Literal["compliant", "partial"]
    exceptions: list[NonEmpty]  # required (non-empty) for partial
    generator_config_version: ResourceId
    validator_config_version: ResourceId
    selection_rule_version: ResourceId
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _targets(self):
        n, q = target_counts(self.page_count)
        if (self.n_target, self.q_min) != (n, q):
            raise ValueError(f"n_target/q_min must be {n}/{q} for page_count={self.page_count}")
        c = self.quota_counts
        if c.general + c.cross_page + c.chart_table + c.confusable != self.n_valid:
            raise ValueError("quota_counts must sum to n_valid (one bucket per question)")
        if self.compliance == "compliant":
            short = [b.value for b in SPECIAL_BUCKETS if getattr(c, b.value) < self.q_min]
            if self.n_valid != self.n_target or short:
                raise ValueError("compliant requires n_valid == n_target and every special bucket >= q_min")
            if self.exceptions:
                raise ValueError("compliant manifests carry no exceptions")
        elif not self.exceptions:
            raise ValueError("partial manifests must list their exceptions")
        return self


def check_manifest(manifest: BenchmarkManifest, questions: list[Question]) -> list[Problem]:
    """Counts in the manifest must match the actual question records."""
    p: list[Problem] = []
    counts = Counter(q.quota_bucket.value for q in questions)
    if len(questions) != manifest.n_valid:
        p.append(Problem("manifest.n_valid", "count_mismatch", f"{manifest.n_valid} != {len(questions)}"))
    for b in QuotaBucket:
        declared = getattr(manifest.quota_counts, b.value)
        if declared != counts.get(b.value, 0):
            p.append(Problem(f"manifest.quota_counts.{b.value}", "count_mismatch",
                             f"{declared} != {counts.get(b.value, 0)}"))
    return p
