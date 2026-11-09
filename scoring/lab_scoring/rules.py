"""Scoring rules, geometry, budget enforcement, and aggregation (SPEC 9.1, 9.3, 16.5)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Optional, Sequence

from lab_contracts.score import ROUNDING_VERSION, MetricResult, display_ratio


def _to_fraction(value: Fraction | int) -> Fraction:
    """Convert an exact int or Fraction to Fraction, rejecting floats and bools."""
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise ValueError(f"expected Fraction or int, got {type(value).__name__}")
    return Fraction(value)


def _scored(
    question_id: str,
    metric: str,
    num: Fraction,
    den: Fraction,
    grader_revision: str,
    support_refs: Sequence[str],
    reason_codes: Sequence[str] = (),
) -> MetricResult:
    """Build a validated scored MetricResult from exact Fraction numerator/denominator (SPEC 16.5)."""
    num_f = _to_fraction(num)
    den_f = _to_fraction(den)
    if num_f < 0 or den_f <= 0 or num_f > den_f:
        raise ValueError(f"invalid scored ratio {num_f}/{den_f}")
    num_float = float(num_f)
    den_float = float(den_f)
    return MetricResult.model_validate(
        {
            "question_id": question_id,
            "metric": metric,
            "status": "scored",
            "raw_value": float(100 * num_f / den_f),
            "display_value": display_ratio(num_float, den_float),
            "rounding": ROUNDING_VERSION,
            "numerator": num_float,
            "denominator": den_float,
            "reason_codes": list(reason_codes),
            "grader_revision": grader_revision,
            "support_refs": list(support_refs),
        }
    )


def _na(
    question_id: str,
    metric: str,
    grader_revision: str,
    reason_codes: Sequence[str],
    support_refs: Sequence[str] = (),
) -> MetricResult:
    """Build a validated not_applicable MetricResult with no numeric values (SPEC 16.5)."""
    reasons = list(reason_codes)
    if not reasons:
        raise ValueError("not_applicable requires non-empty reason_codes")
    return MetricResult.model_validate(
        {
            "question_id": question_id,
            "metric": metric,
            "status": "not_applicable",
            "raw_value": None,
            "display_value": None,
            "rounding": ROUNDING_VERSION,
            "numerator": None,
            "denominator": None,
            "reason_codes": reasons,
            "grader_revision": grader_revision,
            "support_refs": list(support_refs),
        }
    )


def _error(
    question_id: str,
    metric: str,
    grader_revision: str,
    reason_codes: Sequence[str],
    support_refs: Sequence[str] = (),
) -> MetricResult:
    """Build a validated error MetricResult with no numeric values (SPEC 16.5)."""
    reasons = list(reason_codes)
    if not reasons:
        raise ValueError("error requires non-empty reason_codes")
    return MetricResult.model_validate(
        {
            "question_id": question_id,
            "metric": metric,
            "status": "error",
            "raw_value": None,
            "display_value": None,
            "rounding": ROUNDING_VERSION,
            "numerator": None,
            "denominator": None,
            "reason_codes": reasons,
            "grader_revision": grader_revision,
            "support_refs": list(support_refs),
        }
    )


@dataclass(frozen=True)
class Region:
    """Normalized [0, 1] axis-aligned bounding box on a single page (SPEC 9.1)."""

    page_index: int
    x0: Fraction
    y0: Fraction
    x1: Fraction
    y1: Fraction

    def __post_init__(self) -> None:
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int) or self.page_index < 0:
            raise ValueError("page_index must be a non-negative int")
        x0 = _to_fraction(self.x0)
        y0 = _to_fraction(self.y0)
        x1 = _to_fraction(self.x1)
        y1 = _to_fraction(self.y1)
        object.__setattr__(self, "x0", x0)
        object.__setattr__(self, "y0", y0)
        object.__setattr__(self, "x1", x1)
        object.__setattr__(self, "y1", y1)
        if not (Fraction(0) <= x0 < x1 <= Fraction(1)):
            raise ValueError(f"invalid x bounds [{x0}, {x1}] outside [0, 1] or x0 >= x1")
        if not (Fraction(0) <= y0 < y1 <= Fraction(1)):
            raise ValueError(f"invalid y bounds [{y0}, {y1}] outside [0, 1] or y0 >= y1")

    @property
    def area(self) -> Fraction:
        """Exact area of this region."""
        return (self.x1 - self.x0) * (self.y1 - self.y0)


def union_area(regions: list[Region]) -> Fraction:
    """Exact area of the union of axis-aligned rectangles on one page via coordinate compression (SPEC 9.1, T40)."""
    if not regions:
        return Fraction(0)
    pages = {r.page_index for r in regions}
    if len(pages) > 1:
        raise ValueError("union_area requires all regions to be on the same page")

    xs = sorted({r.x0 for r in regions} | {r.x1 for r in regions})
    ys = sorted({r.y0 for r in regions} | {r.y1 for r in regions})

    total = Fraction(0)
    for i in range(len(xs) - 1):
        cx0, cx1 = xs[i], xs[i + 1]
        dx = cx1 - cx0
        x_overlapping = [r for r in regions if r.x0 <= cx0 and cx1 <= r.x1]
        if not x_overlapping:
            continue
        for j in range(len(ys) - 1):
            cy0, cy1 = ys[j], ys[j + 1]
            if any(r.y0 <= cy0 and cy1 <= r.y1 for r in x_overlapping):
                total += dx * (cy1 - cy0)
    return total


def intersection_area(a: list[Region], b: list[Region]) -> Fraction:
    """Exact area of union(a) ∩ union(b) on the same page (SPEC 9.1)."""
    pages = {r.page_index for r in a} | {r.page_index for r in b}
    if len(pages) > 1:
        raise ValueError("intersection_area requires all regions to be on the same page")
    if not a or not b:
        return Fraction(0)

    all_regions = a + b
    xs = sorted({r.x0 for r in all_regions} | {r.x1 for r in all_regions})
    ys = sorted({r.y0 for r in all_regions} | {r.y1 for r in all_regions})

    total = Fraction(0)
    for i in range(len(xs) - 1):
        cx0, cx1 = xs[i], xs[i + 1]
        dx = cx1 - cx0
        a_x = [r for r in a if r.x0 <= cx0 and cx1 <= r.x1]
        b_x = [r for r in b if r.x0 <= cx0 and cx1 <= r.x1]
        if not a_x or not b_x:
            continue
        for j in range(len(ys) - 1):
            cy0, cy1 = ys[j], ys[j + 1]
            if any(r.y0 <= cy0 and cy1 <= r.y1 for r in a_x) and any(
                r.y0 <= cy0 and cy1 <= r.y1 for r in b_x
            ):
                total += dx * (cy1 - cy0)
    return total


def coverage(returned: list[Region], gold: Region) -> Fraction:
    """Fraction of gold region covered by same-page returned regions (SPEC 9.1)."""
    same_page = [r for r in returned if r.page_index == gold.page_index]
    if not same_page:
        return Fraction(0)
    return intersection_area(same_page, [gold]) / gold.area


def unit_hit(
    returned: list[Region],
    gold_regions: list[Region],
    threshold: Fraction = Fraction(4, 5),
) -> bool:
    """True iff every required gold region meets or exceeds the coverage threshold (SPEC 9.1)."""
    if not gold_regions:
        raise ValueError("gold_regions must not be empty")
    thresh = _to_fraction(threshold)
    if not (Fraction(0) < thresh <= Fraction(1)):
        raise ValueError("threshold must be in (0, 1]")
    return all(coverage(returned, g) >= thresh for g in gold_regions)


@dataclass(frozen=True)
class Hit:
    """Ranked retrieval hit with resource usage for budget enforcement (SPEC 9.3)."""

    rank: int
    evidence_tokens: int
    visual_regions: int
    payload: object = None


@dataclass(frozen=True)
class BudgetResult:
    """Result of applying top_k, token, and visual region budgets (SPEC 9.3)."""

    included: list[Hit]
    excluded: list[Hit]
    violation: bool


def apply_budget(
    hits: list[Hit],
    top_k: int,
    max_evidence_tokens: int,
    max_visual_regions: int,
) -> BudgetResult:
    """Include hits in rank order until the first budget-exceeding hit; exclude it and all subsequent hits (SPEC 9.3, T40)."""
    if top_k <= 0 or max_evidence_tokens < 0 or max_visual_regions < 0:
        raise ValueError("invalid budget limits")
    n = len(hits)
    if n == 0:
        return BudgetResult(included=[], excluded=[], violation=False)

    ranks = [h.rank for h in hits]
    if len(set(ranks)) != n or set(ranks) != set(range(1, n + 1)):
        raise ValueError("hit ranks must be unique and contiguous 1..n")
    for h in hits:
        if h.evidence_tokens < 0 or h.visual_regions < 0:
            raise ValueError("hit token and visual region counts must be non-negative")

    ordered = sorted(hits, key=lambda h: h.rank)
    included: list[Hit] = []
    cum_tokens = 0
    cum_regions = 0

    for idx, hit in enumerate(ordered):
        if (
            len(included) + 1 > top_k
            or cum_tokens + hit.evidence_tokens > max_evidence_tokens
            or cum_regions + hit.visual_regions > max_visual_regions
        ):
            return BudgetResult(
                included=included,
                excluded=ordered[idx:],
                violation=True,
            )
        included.append(hit)
        cum_tokens += hit.evidence_tokens
        cum_regions += hit.visual_regions

    return BudgetResult(included=included, excluded=[], violation=False)


def _validate_gold_sets(gold_sets: list[set[str]]) -> None:
    if not gold_sets or any(not g for g in gold_sets):
        raise ValueError("gold_sets must be non-empty and contain no empty sets")


def recall_at_k(
    question_id: str,
    gold_sets: list[set[str]],
    matched: set[str],
    grader_revision: str,
) -> MetricResult:
    """Recall@k = 100 * max_j(|M ∩ G_j| / |G_j|); ties broken by smaller |G_j| (SPEC 16.5, T37)."""
    _validate_gold_sets(gold_sets)
    best_ratio: Optional[Fraction] = None
    best_num = Fraction(0)
    best_den = Fraction(1)
    best_refs: list[str] = []

    for g in gold_sets:
        inter = sorted(matched & g)
        num = Fraction(len(inter))
        den = Fraction(len(g))
        ratio = num / den
        if (
            best_ratio is None
            or ratio > best_ratio
            or (ratio == best_ratio and den < best_den)
        ):
            best_ratio = ratio
            best_num = num
            best_den = den
            best_refs = inter

    return _scored(
        question_id=question_id,
        metric="recall_at_k",
        num=best_num,
        den=best_den,
        grader_revision=grader_revision,
        support_refs=best_refs,
    )


def complete_at_k(
    question_id: str,
    gold_sets: list[set[str]],
    matched: set[str],
    grader_revision: str,
) -> MetricResult:
    """Complete@k = 100 if some G_j ⊆ M else 0 (SPEC 16.5, T37)."""
    _validate_gold_sets(gold_sets)
    satisfied = [g for g in gold_sets if g.issubset(matched)]
    if satisfied:
        best_g = min(satisfied, key=lambda g: (len(g), sorted(g)))
        return _scored(
            question_id=question_id,
            metric="complete_at_k",
            num=Fraction(1),
            den=Fraction(1),
            grader_revision=grader_revision,
            support_refs=sorted(best_g),
        )
    return _scored(
        question_id=question_id,
        metric="complete_at_k",
        num=Fraction(0),
        den=Fraction(1),
        grader_revision=grader_revision,
        support_refs=[],
    )


def correctness(
    question_id: str,
    facts: list[tuple[Fraction, Fraction]],
    partial_rubric_defined: bool,
    grader_revision: str,
    abstained: bool = False,
) -> MetricResult:
    """Weighted fact correctness over {0, 1/2, 1} (SPEC 16.5, T38, T42)."""
    if not facts:
        raise ValueError("facts must not be empty")

    allowed_scores = {Fraction(0), Fraction(1, 2), Fraction(1)}
    parsed_facts: list[tuple[Fraction, Fraction]] = []
    has_partial = False

    for weight, score in facts:
        w = _to_fraction(weight)
        s = _to_fraction(score)
        if w <= 0:
            raise ValueError("fact weight must be > 0")
        if s not in allowed_scores:
            raise ValueError(f"fact score must be in {{0, 1/2, 1}}, got {s}")
        if s == Fraction(1, 2):
            has_partial = True
        parsed_facts.append((w, s))

    if has_partial and not partial_rubric_defined:
        return _error(
            question_id=question_id,
            metric="correctness",
            grader_revision=grader_revision,
            reason_codes=["partial_score_without_rubric"],
        )

    total_weight = sum((w for w, _ in parsed_facts), Fraction(0))
    if abstained:
        return _scored(
            question_id=question_id,
            metric="correctness",
            num=Fraction(0),
            den=total_weight,
            grader_revision=grader_revision,
            support_refs=[],
            reason_codes=["abstained"],
        )

    weighted_sum = sum((w * s for w, s in parsed_facts), Fraction(0))
    return _scored(
        question_id=question_id,
        metric="correctness",
        num=weighted_sum,
        den=total_weight,
        grader_revision=grader_revision,
        support_refs=[],
    )


def textual_consistency(
    question_id: str,
    passed: int,
    applicable: int,
    grader_revision: str,
    method_failed: bool = False,
) -> MetricResult:
    """TextualConsistency = 100 * passed / applicable, or N/A if applicable == 0 (SPEC 16.5, T39)."""
    if applicable < 0 or passed < 0 or passed > applicable:
        raise ValueError("require 0 <= passed <= applicable")
    if applicable == 0:
        return _na(
            question_id=question_id,
            metric="textual_consistency",
            grader_revision=grader_revision,
            reason_codes=["no_applicable_checks"],
        )
    if method_failed:
        return _scored(
            question_id=question_id,
            metric="textual_consistency",
            num=Fraction(0),
            den=Fraction(applicable),
            grader_revision=grader_revision,
            support_refs=[],
            reason_codes=["method_failed"],
        )
    return _scored(
        question_id=question_id,
        metric="textual_consistency",
        num=Fraction(passed),
        den=Fraction(applicable),
        grader_revision=grader_revision,
        support_refs=[],
    )


def localization_precision(
    question_id: str,
    returned: list[Region],
    gold: list[Region],
    grader_revision: str,
) -> MetricResult:
    """LocalizationPrecision = 100 * sum_page area(R ∩ G) / sum_page area(R) (SPEC 16.5)."""
    if not returned:
        return _scored(
            question_id=question_id,
            metric="localization_precision",
            num=Fraction(0),
            den=Fraction(1),
            grader_revision=grader_revision,
            support_refs=[],
            reason_codes=["no_valid_regions"],
        )

    returned_by_page: dict[int, list[Region]] = {}
    for r in returned:
        returned_by_page.setdefault(r.page_index, []).append(r)

    gold_by_page: dict[int, list[Region]] = {}
    for g in gold:
        gold_by_page.setdefault(g.page_index, []).append(g)

    num = Fraction(0)
    den = Fraction(0)
    for page, page_ret in sorted(returned_by_page.items()):
        den += union_area(page_ret)
        page_gold = gold_by_page.get(page, [])
        if page_gold:
            num += intersection_area(page_ret, page_gold)

    return _scored(
        question_id=question_id,
        metric="localization_precision",
        num=num,
        den=den,
        grader_revision=grader_revision,
        support_refs=[],
    )


def citation_precision(
    question_id: str,
    citations: list[tuple[str, frozenset[str], bool]],
    answerable: bool,
    grader_revision: str,
) -> MetricResult:
    """CitationPrecision over deduplicated (claim_id, region_key) pairs (SPEC 16.5, T40)."""
    deduped: dict[tuple[str, frozenset[str]], bool] = {}
    for claim_id, region_key, supports in citations:
        if not claim_id or not region_key:
            raise ValueError("citation claim_id and region_key must be non-empty")
        key = (claim_id, frozenset(region_key))
        flag = bool(supports)
        if key in deduped and deduped[key] != flag:
            raise ValueError(f"conflicting supports value for citation {key}")
        deduped[key] = flag

    if not deduped:
        if answerable:
            return _scored(
                question_id=question_id,
                metric="citation_precision",
                num=Fraction(0),
                den=Fraction(1),
                grader_revision=grader_revision,
                support_refs=[],
                reason_codes=["no_citations"],
            )
        return _na(
            question_id=question_id,
            metric="citation_precision",
            grader_revision=grader_revision,
            reason_codes=["not_answerable"],
        )

    supported = sum(1 for v in deduped.values() if v)
    return _scored(
        question_id=question_id,
        metric="citation_precision",
        num=Fraction(supported),
        den=Fraction(len(deduped)),
        grader_revision=grader_revision,
        support_refs=[],
    )


def citation_completeness(
    question_id: str,
    claims_total: int,
    claims_fully_cited: int,
    grader_revision: str,
    abstained: bool = False,
) -> MetricResult:
    """CitationCompleteness = 100 * claims_fully_cited / claims_total (SPEC 16.5, T42)."""
    if claims_total < 0 or claims_fully_cited < 0 or claims_fully_cited > claims_total:
        raise ValueError("require 0 <= claims_fully_cited <= claims_total")
    if abstained:
        return _scored(
            question_id=question_id,
            metric="citation_completeness",
            num=Fraction(0),
            den=Fraction(1),
            grader_revision=grader_revision,
            support_refs=[],
            reason_codes=["abstained"],
        )
    if claims_total == 0:
        return _na(
            question_id=question_id,
            metric="citation_completeness",
            grader_revision=grader_revision,
            reason_codes=["no_verifiable_claims"],
        )
    return _scored(
        question_id=question_id,
        metric="citation_completeness",
        num=Fraction(claims_fully_cited),
        den=Fraction(claims_total),
        grader_revision=grader_revision,
        support_refs=[],
    )


def faithfulness(
    question_id: str,
    claims_total: int,
    claims_supported: int,
    grader_revision: str,
    abstained: bool = False,
) -> MetricResult:
    """Faithfulness = 100 * claims_supported / claims_total (SPEC 16.5, T42)."""
    if claims_total < 0 or claims_supported < 0 or claims_supported > claims_total:
        raise ValueError("require 0 <= claims_supported <= claims_total")
    if abstained:
        return _scored(
            question_id=question_id,
            metric="faithfulness",
            num=Fraction(0),
            den=Fraction(1),
            grader_revision=grader_revision,
            support_refs=[],
            reason_codes=["abstained"],
        )
    if claims_total == 0:
        return _na(
            question_id=question_id,
            metric="faithfulness",
            grader_revision=grader_revision,
            reason_codes=["no_verifiable_claims"],
        )
    return _scored(
        question_id=question_id,
        metric="faithfulness",
        num=Fraction(claims_supported),
        den=Fraction(claims_total),
        grader_revision=grader_revision,
        support_refs=[],
    )


@dataclass(frozen=True)
class Aggregate:
    """Aggregated metric summary across questions (SPEC 16.5, T43)."""

    total: int
    scored: int
    not_applicable: int
    pending: int
    error: int
    failed_method: int
    provisional: bool
    mean: Optional[Fraction]


def aggregate(
    metric: str,
    results: list[MetricResult],
    method_failed_question_ids: set[str],
) -> Aggregate:
    """Aggregate per-question MetricResults using exact Fraction arithmetic (SPEC 16.5, T43)."""
    by_qid: dict[str, MetricResult] = {}
    for r in results:
        if r.metric != metric:
            raise ValueError(f"expected metric {metric!r}, got {r.metric!r}")
        if r.question_id in by_qid:
            raise ValueError(f"duplicate question_id {r.question_id!r}")
        by_qid[r.question_id] = r

    for qid in method_failed_question_ids:
        if qid not in by_qid:
            raise ValueError(f"method_failed question_id {qid!r} missing from results")
        failed_res = by_qid[qid]
        if failed_res.status != "scored" or failed_res.numerator != 0.0:
            raise ValueError(
                f"method_failed question_id {qid!r} must have a scored 0 result"
            )

    scored_values: list[Fraction] = []
    not_applicable = 0
    pending = 0
    error = 0

    for r in results:
        if r.status == "scored":
            assert r.numerator is not None and r.denominator is not None
            num = Fraction(Decimal(repr(r.numerator)))
            den = Fraction(Decimal(repr(r.denominator)))
            scored_values.append(Fraction(100) * num / den)
        elif r.status == "not_applicable":
            not_applicable += 1
        elif r.status == "pending":
            pending += 1
        elif r.status == "error":
            error += 1

    scored_count = len(scored_values)
    mean = (
        sum(scored_values, Fraction(0)) / Fraction(scored_count)
        if scored_count > 0
        else None
    )

    return Aggregate(
        total=len(results),
        scored=scored_count,
        not_applicable=not_applicable,
        pending=pending,
        error=error,
        failed_method=len(method_failed_question_ids),
        provisional=(pending + error) > 0,
        mean=mean,
    )
