"""Deterministic draft candidate selection (SPEC 5.1, R04/R05, T14–T16).

This selects records for further verification, never a frozen benchmark.
Contract consistency is necessary, not proof of answer quality, visual
necessity, human review or semantic deduplication. Those gates remain with
the independent verifier and publishing workflow.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from .benchmark import (
    SPECIAL_BUCKETS, EvidenceUnit, Problem, Question, QuotaBucket,
    SemanticAnswer, SourceAnswer, TextualAnswer, ValidationRecord,
    target_counts, validate_bundle,
)

SELECTION_RULE_VERSION = "draft-quota-matching-1"


class InvalidCandidatePool(ValueError):
    """The located contract problems that prevent draft selection."""

    def __init__(self, problems: list[Problem]):
        self.problems = tuple(problems)
        super().__init__("; ".join(f"{p.path}: {p.code}" for p in problems))


@dataclass(frozen=True)
class BucketPlan:
    bucket: QuotaBucket
    selected: int
    minimum: int
    deficit: int


@dataclass(frozen=True)
class CandidatePlan:
    """Only IDs/counts: no gold projection, generated content or freeze state.

    ``planned`` means a structurally consistent selection meets the count
    policy. It is deliberately not a compliance or publication verdict.
    ``omitted_ids`` includes duplicate-hash aliases and surplus candidates.
    """

    selection_rule_version: str
    n_target: int
    q_min: int
    selected_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]
    buckets: tuple[BucketPlan, ...]
    question_deficit: int
    status: Literal["planned", "needs_review"]


def plan_candidates(
    page_count: int,
    questions: list[Question],
    semantic_answers: list[SemanticAnswer],
    textual_answers: list[TextualAnswer],
    source_answers: list[SourceAnswer],
    evidence: dict[str, EvidenceUnit],
    validations: dict[str, ValidationRecord],
) -> CandidatePlan:
    """Select at most N candidates, one declared bucket per distinct hash.

    Validate all four input parts and references, including candidates that
    will not be selected. Duplicate question hashes are allowed *in the
    pool*, so aliases can compete for different declared buckets; they can
    never both enter the plan. Other bundle problems fail closed.

    Match the three Q-sized special-bucket requirements before filling to
    N. An augmenting path can move an earlier alias to another bucket, so a
    feasible independent assignment is not lost to greedy deduplication.
    Tie breaks use (question_hash, ordinal, question_id), never input order.
    Existing question tags, buckets, ordinals and all answer records remain
    untouched. The selected IDs join the four parts without array indexing.
    """
    n, q_min = target_counts(page_count)
    for name, container, expected in (
        ("questions", questions, (list, tuple)),
        ("semantic_answers", semantic_answers, (list, tuple)),
        ("textual_answers", textual_answers, (list, tuple)),
        ("source_answers", source_answers, (list, tuple)),
        ("evidence", evidence, dict),
        ("validations", validations, dict),
    ):
        if not isinstance(container, expected):
            raise TypeError(f"{name} must be a {expected.__name__ if isinstance(expected, type) else 'sequence'}")
    for records, model in (
        (questions, Question), (semantic_answers, SemanticAnswer),
        (textual_answers, TextualAnswer), (source_answers, SourceAnswer),
        (list(evidence.values()), EvidenceUnit),
        (list(validations.values()), ValidationRecord),
    ):
        if any(not isinstance(record, model) for record in records):
            raise TypeError(f"candidate pool requires validated {model.__name__} records")
    problems = [p for p in validate_bundle(
        questions, semantic_answers, textual_answers, source_answers,
        evidence, validations,
    ) if p.code != "duplicate_question"]
    for key in sorted(evidence):
        unit = evidence[key]
        for index, region in enumerate(unit.regions):
            if region.page_index >= page_count:
                problems.append(Problem(
                    f"evidence[{key}].regions[{index}].page_index",
                    "page_out_of_document", f"{region.page_index} >= {page_count}",
                ))
    if problems:
        raise InvalidCandidatePool(problems)

    # Each hash may offer several bucket-specific aliases, but owns only
    # one position. A stable representative exists for each (hash, bucket).
    aliases: dict[str, dict[QuotaBucket, Question]] = {}
    ordered = sorted(questions, key=lambda item: (item.question_hash, item.ordinal, item.question_id))
    for question in ordered:
        aliases.setdefault(question.question_hash, {}).setdefault(question.quota_bucket, question)
    available = {
        bucket: tuple(key for key in sorted(aliases) if bucket in aliases[key])
        for bucket in SPECIAL_BUCKETS
    }
    assigned: dict[str, QuotaBucket] = {}

    def augment(bucket: QuotaBucket, seen: set[QuotaBucket]) -> bool:
        # Three special buckets cap the simple path length in the bucket
        # graph; do not unmark failed buckets within one search.
        if bucket in seen:
            return False
        seen.add(bucket)
        for key in available[bucket]:
            if assigned.get(key) == bucket:
                continue
            previous = assigned.get(key)
            if previous is None or augment(previous, seen):
                assigned[key] = bucket
                return True
        return False

    for bucket in SPECIAL_BUCKETS:
        for _ in range(q_min):
            if not augment(bucket, set()):
                break

    selected = {key: aliases[key][bucket] for key, bucket in assigned.items()}
    for key in sorted(aliases):
        if len(selected) == n:
            break
        if key not in selected:
            selected[key] = min(aliases[key].values(), key=lambda item: (item.ordinal, item.question_id))
    records = sorted(selected.values(), key=lambda item: (item.ordinal, item.question_id))
    selected_ids = tuple(item.question_id for item in records)
    chosen = set(selected_ids)
    counts = Counter(item.quota_bucket for item in records)
    buckets = tuple(
        BucketPlan(bucket, counts[bucket], q_min if bucket in SPECIAL_BUCKETS else 0,
                   max(0, q_min - counts[bucket]) if bucket in SPECIAL_BUCKETS else 0)
        for bucket in QuotaBucket
    )
    deficit = max(0, n - len(records))
    return CandidatePlan(
        SELECTION_RULE_VERSION, n, q_min, selected_ids,
        tuple(item.question_id for item in sorted(questions, key=lambda item: (item.ordinal, item.question_id))
              if item.question_id not in chosen),
        buckets, deficit,
        "needs_review" if deficit or any(bucket.deficit for bucket in buckets) else "planned",
    )
