"""Visual and cross-page necessity gate, and chart-quota coverage (SPEC 19.5, T17, T20).

Pure functions over the benchmark contract; no provider call and no frozen data
is changed. Contract validation only floors a chart/table question at "has
visual evidence" (``benchmark.validate_bundle``); whether the question really
needs the figure is established by removing evidence and asking an independent
verifier whether it is still answerable. This module decides which removals are
required and what their results mean:

* ``chart_table`` questions: removing all visual evidence (table, figure, image)
  must make the question unanswerable; if it stays answerable it is not counted
  as a chart question (SPEC: "只看 caption／周邊文字即可回答者不計讀圖配額").
* ``cross_page`` questions: removing the evidence of any single page must make
  it unanswerable; evidence on one page only, or any page that can be dropped,
  disqualifies it (T17, T20).
* ``image_text_integration`` questions (any bucket): removing the visual
  evidence and, separately, the text evidence must each make it unanswerable.
* A chart question whose reasoning type is ``cross_page_integration`` needs the
  visual removal and every page removal.

Results from the same model's self-review are never evidence (SPEC: "同一模型
同輪自評不是獨立證據"); with no independent result for a required removal the
verdict is ``needs_review``, never a pass. Conflicting independent results are
kept as ``needs_review``. A verdict only says whether the question may count
toward its quota; reclassifying is a hint for the planner, not an edit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .benchmark import EvidenceUnit, Question, QuotaBucket, ReasoningType, SourceAnswer
from .source import RegionKind

__all__ = [
    "Ablation",
    "AblationResult",
    "CoverageReport",
    "NecessityDecision",
    "chart_coverage",
    "decide_necessity",
    "required_ablations",
]

VISUAL_KINDS = frozenset({RegionKind.table, RegionKind.figure, RegionKind.image})
TEXT_KINDS = frozenset({RegionKind.text, RegionKind.formula})
MIN_REASONING_TYPES = 3


@dataclass(frozen=True)
class Ablation:
    """One required removal: ``kind`` is ``visual``, ``text`` or ``page:<index>``."""

    kind: str
    removed: frozenset[str]


@dataclass(frozen=True)
class AblationResult:
    """An independent verifier's answer to "is the question still fully answerable
    with these evidence units removed?". ``independent`` is False for the same
    model reviewing its own question in the same round."""

    question_id: str
    removed: frozenset[str]
    answerable: bool
    verifier_id: str
    independent: bool


@dataclass(frozen=True)
class NecessityDecision:
    question_id: str
    verdict: str  # necessary | not_necessary | needs_review | not_required
    reasons: tuple[str, ...]
    reclassify_to: Optional[str] = None

    @property
    def passes_gate(self) -> bool:
        """May the question count toward its special quota?"""
        return self.verdict in ("necessary", "not_required")


def _evidence_ids(source: SourceAnswer, evidence: Mapping[str, EvidenceUnit]) -> list[str]:
    seen: dict[str, None] = {}
    for evidence_set in source.evidence_sets:
        for evidence_id in evidence_set.evidence_ids:
            if evidence_id not in evidence:
                raise ValueError("unknown_evidence")
            seen.setdefault(evidence_id)
    return list(seen)


def _is_visual(unit: EvidenceUnit) -> bool:
    return any(r.kind in VISUAL_KINDS for r in unit.regions)


def _is_text_only(unit: EvidenceUnit) -> bool:
    return not _is_visual(unit) and any(r.kind in TEXT_KINDS for r in unit.regions)


def _pages(unit: EvidenceUnit) -> set[int]:
    return {r.page_index for r in unit.regions}


def required_ablations(
    question: Question, source: SourceAnswer, evidence: Mapping[str, EvidenceUnit]
) -> tuple[Ablation, ...]:
    """The removals that must each make ``question`` unanswerable. Empty when the
    question is not subject to the gate. A required removal with nothing to
    remove (no visual or no text evidence) is returned with an empty ``removed``
    set so the decision can explain it."""
    if source.question_id != question.question_id:
        raise ValueError("source_answer_for_other_question")
    ids = _evidence_ids(source, evidence)
    wants_visual = (
        question.quota_bucket is QuotaBucket.chart_table
        or question.reasoning_type is ReasoningType.image_text_integration
    )
    wants_text = question.reasoning_type is ReasoningType.image_text_integration
    wants_pages = question.quota_bucket is QuotaBucket.cross_page or (
        question.quota_bucket is QuotaBucket.chart_table
        and question.reasoning_type is ReasoningType.cross_page_integration
    )
    out: list[Ablation] = []
    if wants_visual:
        out.append(Ablation("visual", frozenset(i for i in ids if _is_visual(evidence[i]))))
    if wants_text:
        out.append(Ablation("text", frozenset(i for i in ids if _is_text_only(evidence[i]))))
    if wants_pages:
        pages = sorted({p for i in ids for p in _pages(evidence[i])})
        for page in pages:
            out.append(Ablation(f"page:{page}", frozenset(i for i in ids if page in _pages(evidence[i]))))
    return tuple(out)


def decide_necessity(
    question: Question,
    source: SourceAnswer,
    evidence: Mapping[str, EvidenceUnit],
    results: Iterable[AblationResult],
) -> NecessityDecision:
    required = required_ablations(question, source, evidence)
    qid = question.question_id
    if not required:
        return NecessityDecision(qid, "not_required", ())

    reasons: list[str] = []
    not_necessary = False
    review = False

    empty = [a for a in required if not a.removed]
    for a in empty:
        reasons.append(f"nothing_to_remove:{a.kind}")
    if any(a.kind in ("visual", "text") for a in empty):
        # No evidence of that kind at all: the question cannot need it.
        not_necessary = True
    pages = [a for a in required if a.kind.startswith("page:")]
    if question.quota_bucket is QuotaBucket.cross_page and len(pages) < 2:
        reasons.append("single_page_evidence")
        not_necessary = True

    mine = [r for r in results if r.question_id == qid]
    if any(not r.independent for r in mine):
        reasons.append("self_review_ignored")
    for a in required:
        if not a.removed:
            continue
        relevant = [r for r in mine if r.independent and r.removed == a.removed]
        if not relevant:
            reasons.append(f"missing_ablation:{a.kind}")
            review = True
            continue
        outcomes = {r.answerable for r in relevant}
        if len(outcomes) > 1:
            reasons.append(f"conflicting_ablation:{a.kind}")
            review = True
        elif outcomes == {True}:
            reasons.append(f"answerable_without:{a.kind}")
            not_necessary = True

    if not_necessary:
        return NecessityDecision(qid, "not_necessary", tuple(reasons), reclassify_to="general")
    if review:
        return NecessityDecision(qid, "needs_review", tuple(reasons))
    return NecessityDecision(qid, "necessary", tuple(reasons))


@dataclass(frozen=True)
class CoverageReport:
    quota: int
    qualifying: int
    shortfall: int
    reasoning_types: tuple[tuple[str, int], ...]
    required_distinct_types: int
    has_image_text_integration: bool
    has_cross_page_visual: bool
    material_sufficient: bool
    public_gaps: tuple[str, ...] = field(default_factory=tuple)
    compliant: bool = False


def chart_coverage(
    chart_quota: int,
    questions: Iterable[Question],
    decisions: Mapping[str, NecessityDecision],
    *,
    material_sufficient: bool,
) -> CoverageReport:
    """Coverage inside the chart quota (SPEC 19.5): at least ``min(3, Q)`` distinct
    reasoning types; with enough material also one necessary image-text
    integration question and one necessary cross-page visual question (they may
    be the same question). Only chart questions whose necessity is ``necessary``
    count. Unmet items are returned as ``public_gaps`` (never filled with
    invented questions); without enough material the two integration items are
    reported but do not make the result non-compliant."""
    if isinstance(chart_quota, bool) or not isinstance(chart_quota, int) or chart_quota < 1:
        raise ValueError("chart_quota must be a positive integer")
    qualifying = [
        q for q in questions
        if q.quota_bucket is QuotaBucket.chart_table
        and decisions.get(q.question_id) is not None
        and decisions[q.question_id].verdict == "necessary"
    ]
    counts: dict[str, int] = {}
    for q in qualifying:
        counts[q.reasoning_type.value] = counts.get(q.reasoning_type.value, 0) + 1
    required_types = min(MIN_REASONING_TYPES, chart_quota)
    has_integration = any(q.reasoning_type is ReasoningType.image_text_integration for q in qualifying)
    has_cross = any(q.reasoning_type is ReasoningType.cross_page_integration for q in qualifying)

    gaps: list[str] = []
    shortfall = max(0, chart_quota - len(qualifying))
    if shortfall:
        gaps.append("chart_quota_shortfall")
    diversity_ok = len(counts) >= required_types
    if not diversity_ok:
        gaps.append("reasoning_type_diversity")
    if not has_integration:
        gaps.append("image_text_integration_missing")
    if not has_cross:
        gaps.append("cross_page_visual_missing")

    integration_ok = (has_integration and has_cross) or not material_sufficient
    return CoverageReport(
        quota=chart_quota,
        qualifying=len(qualifying),
        shortfall=shortfall,
        reasoning_types=tuple(sorted(counts.items())),
        required_distinct_types=required_types,
        has_image_text_integration=has_integration,
        has_cross_page_visual=has_cross,
        material_sufficient=material_sufficient,
        public_gaps=tuple(gaps),
        compliant=shortfall == 0 and diversity_ok and integration_ok,
    )
