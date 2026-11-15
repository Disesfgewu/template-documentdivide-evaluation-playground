"""Deterministic generation and quota control logic (SPEC 12.2).

PURPOSE:
- QuestionGeneration: validate_sources -> validate_answer -> classify_candidate.
  classify_candidate routes candidate questions to accept, regenerate, or needs_review.
  The number of regenerations and total cost have limits; reaching a limit goes to needs_review.
- BenchmarkFinalization: check_quotas finds deficits and schedules new question work,
  keeping existing qualified questions.

Existing qualified questions are never part of the input and are never touched:
the result only ever ADDS work. This module is the DETERMINISTIC DECISION LOGIC only.
It never calls a model, never creates work, and never persists state. It returns
decisions the workflow executes.
"""
from __future__ import annotations

from collections.abc import Sequence
import dataclasses
from dataclasses import dataclass
import re
from typing import Literal

CONTROL_RULE_VERSION = "gen-control-1"

NON_REGENERABLE: frozenset[str] = frozenset({
    "gold_leak",
    "policy_violation",
    "unsupported_document",
    "source_unavailable",
})

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class InvalidControlInput(ValueError):
    """Raised when control input violates validation rules."""


@dataclass(frozen=True)
class Policy:
    """Generation policy capping extra attempts and budget per question."""

    max_regenerations: int          # >= 0 ; number of EXTRA generation attempts after the first
    cost_limit_micros: int          # > 0 ; total allowed for this question: spent + reserved + next estimate must be <= this
    currency: str                   # non-empty
    pricing_revision: str           # non-empty

    def __post_init__(self) -> None:
        if isinstance(self.max_regenerations, bool) or not isinstance(self.max_regenerations, int) or self.max_regenerations < 0:
            raise InvalidControlInput("max_regenerations must be a non-negative int")
        if isinstance(self.cost_limit_micros, bool) or not isinstance(self.cost_limit_micros, int) or self.cost_limit_micros <= 0:
            raise InvalidControlInput("cost_limit_micros must be a positive int")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise InvalidControlInput("currency must be a non-empty string")
        if not isinstance(self.pricing_revision, str) or not self.pricing_revision.strip():
            raise InvalidControlInput("pricing_revision must be a non-empty string")


@dataclass(frozen=True)
class AttemptState:
    """Cost and attempt tracking state for a question candidate."""

    regenerations_used: int         # >= 0
    spent_micros: int               # >= 0 settled
    reserved_micros: int            # >= 0 held, including outcome_unknown reservations
    unknown_micros: int             # >= 0, the part of reserved_micros held by outcome_unknown calls (unknown_micros <= reserved_micros)
    currency: str
    pricing_revision: str

    def __post_init__(self) -> None:
        if isinstance(self.regenerations_used, bool) or not isinstance(self.regenerations_used, int) or self.regenerations_used < 0:
            raise InvalidControlInput("regenerations_used must be a non-negative int")
        if isinstance(self.spent_micros, bool) or not isinstance(self.spent_micros, int) or self.spent_micros < 0:
            raise InvalidControlInput("spent_micros must be a non-negative int")
        if isinstance(self.reserved_micros, bool) or not isinstance(self.reserved_micros, int) or self.reserved_micros < 0:
            raise InvalidControlInput("reserved_micros must be a non-negative int")
        if isinstance(self.unknown_micros, bool) or not isinstance(self.unknown_micros, int) or self.unknown_micros < 0:
            raise InvalidControlInput("unknown_micros must be a non-negative int")
        if self.unknown_micros > self.reserved_micros:
            raise InvalidControlInput("unknown_micros must not exceed reserved_micros")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise InvalidControlInput("currency must be a non-empty string")
        if not isinstance(self.pricing_revision, str) or not self.pricing_revision.strip():
            raise InvalidControlInput("pricing_revision must be a non-empty string")


@dataclass(frozen=True)
class Outcome:
    """Result of validate_sources + validate_answer for ONE candidate."""

    source_valid: bool
    answer_valid: bool
    failure_codes: tuple[str, ...] = ()
    blocking_flags: tuple[str, ...] = ()  # unique; any order (the decision sorts them)

    def __post_init__(self) -> None:
        if not isinstance(self.source_valid, bool):
            raise InvalidControlInput("source_valid must be a bool")
        if not isinstance(self.answer_valid, bool):
            raise InvalidControlInput("answer_valid must be a bool")

        if not isinstance(self.failure_codes, tuple):
            raise InvalidControlInput("failure_codes must be a tuple")
        for code in self.failure_codes:
            if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
                raise InvalidControlInput(f"invalid failure code format: {code!r}")
        if tuple(sorted(set(self.failure_codes))) != self.failure_codes:
            raise InvalidControlInput("failure_codes must be sorted and unique")
        if self.source_valid and self.answer_valid and self.failure_codes:
            raise InvalidControlInput("failure_codes must be empty when candidate is valid")

        if not isinstance(self.blocking_flags, tuple):
            raise InvalidControlInput("blocking_flags must be a tuple")
        for flag in self.blocking_flags:
            if not isinstance(flag, str) or not _CODE_PATTERN.fullmatch(flag):
                raise InvalidControlInput(f"invalid blocking flag format: {flag!r}")
        if len(set(self.blocking_flags)) != len(self.blocking_flags):
            raise InvalidControlInput("blocking_flags must be unique")


@dataclass(frozen=True)
class Decision:
    """Classification decision for candidate question generation."""

    route: Literal["accept", "regenerate", "needs_review"]
    reasons: tuple[str, ...]
    next_state: AttemptState
    rule_version: str

    def __post_init__(self) -> None:
        if self.route not in ("accept", "regenerate", "needs_review"):
            raise InvalidControlInput(f"invalid route: {self.route!r}")
        if not isinstance(self.reasons, tuple):
            raise InvalidControlInput("reasons must be a tuple")
        if any(not isinstance(r, str) for r in self.reasons):
            raise InvalidControlInput("reasons elements must be strings")
        if not isinstance(self.next_state, AttemptState):
            raise InvalidControlInput("next_state must be an AttemptState")
        if not isinstance(self.rule_version, str) or not self.rule_version:
            raise InvalidControlInput("rule_version must be a non-empty string")


def classify_candidate(
    policy: Policy,
    state: AttemptState,
    outcome: Outcome,
    next_estimate_micros: int,
) -> Decision:
    """Classify candidate question according to validation outcome and limits.

    Decision order (first match wins):
    1. Both source and answer valid:
       - if blocking_flags: route 'needs_review', reasons = ('blocking_flag', ...flags) sorted
       - else: route 'accept', reasons = ()
    2. Invalid candidate:
       - if failure_codes is empty: raises InvalidControlInput
       - if any failure code in NON_REGENERABLE: route 'needs_review', reasons = sorted(codes ∩ NON_REGENERABLE | {'not_regenerable'})
       - elif regenerations_used >= policy.max_regenerations: route 'needs_review', reasons = ('regenerate_limit',)
       - elif unknown_micros > 0: route 'needs_review', reasons = ('cost_unknown_unreconciled',)
       - elif spent + reserved + next_estimate > cost_limit: route 'needs_review', reasons = ('budget_exhausted',)
       - else: route 'regenerate', reasons = tuple(sorted(set(failure_codes))), next_state = replace(state, regenerations_used+1)
    """
    if not isinstance(policy, Policy):
        raise InvalidControlInput("policy must be a Policy")
    if not isinstance(state, AttemptState):
        raise InvalidControlInput("state must be an AttemptState")
    if not isinstance(outcome, Outcome):
        raise InvalidControlInput("outcome must be an Outcome")
    if isinstance(next_estimate_micros, bool) or not isinstance(next_estimate_micros, int) or next_estimate_micros <= 0:
        raise InvalidControlInput("next_estimate_micros must be a positive int")
    if state.currency != policy.currency or state.pricing_revision != policy.pricing_revision:
        raise InvalidControlInput("currency/pricing revision mismatch")
    if state.unknown_micros > state.reserved_micros:
        raise InvalidControlInput("unknown_micros must not exceed reserved_micros")

    if outcome.source_valid and outcome.answer_valid:
        if outcome.blocking_flags:
            reasons = tuple(sorted(set(("blocking_flag",) + outcome.blocking_flags)))
            return Decision(
                route="needs_review",
                reasons=reasons,
                next_state=state,
                rule_version=CONTROL_RULE_VERSION,
            )
        return Decision(
            route="accept",
            reasons=(),
            next_state=state,
            rule_version=CONTROL_RULE_VERSION,
        )

    if not outcome.failure_codes:
        raise InvalidControlInput("invalid candidate needs failure codes")

    failure_codes = outcome.failure_codes
    non_regen = set(failure_codes) & NON_REGENERABLE
    if non_regen:
        reasons = tuple(sorted(non_regen | {"not_regenerable"}))
        return Decision(
            route="needs_review",
            reasons=reasons,
            next_state=state,
            rule_version=CONTROL_RULE_VERSION,
        )

    if state.regenerations_used >= policy.max_regenerations:
        return Decision(
            route="needs_review",
            reasons=("regenerate_limit",),
            next_state=state,
            rule_version=CONTROL_RULE_VERSION,
        )

    if state.unknown_micros > 0:
        return Decision(
            route="needs_review",
            reasons=("cost_unknown_unreconciled",),
            next_state=state,
            rule_version=CONTROL_RULE_VERSION,
        )

    if state.spent_micros + state.reserved_micros + next_estimate_micros > policy.cost_limit_micros:
        return Decision(
            route="needs_review",
            reasons=("budget_exhausted",),
            next_state=state,
            rule_version=CONTROL_RULE_VERSION,
        )

    next_state = dataclasses.replace(state, regenerations_used=state.regenerations_used + 1)
    return Decision(
        route="regenerate",
        reasons=tuple(sorted(set(failure_codes))),
        next_state=next_state,
        rule_version=CONTROL_RULE_VERSION,
    )


@dataclass(frozen=True)
class BucketDeficit:
    """Bucket identifier and current question deficit."""

    bucket: str
    deficit: int                    # >= 0

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, str) or not self.bucket.strip():
            raise InvalidControlInput("bucket must be a non-empty string")
        if isinstance(self.deficit, bool) or not isinstance(self.deficit, int) or self.deficit < 0:
            raise InvalidControlInput("deficit must be a non-negative int")


@dataclass(frozen=True)
class QuotaPolicy:
    """Caps per scheduling round across buckets."""

    max_rounds: int                 # >= 0
    max_new_per_round: int          # >= 1

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int) or self.max_rounds < 0:
            raise InvalidControlInput("max_rounds must be a non-negative int")
        if isinstance(self.max_new_per_round, bool) or not isinstance(self.max_new_per_round, int) or self.max_new_per_round < 1:
            raise InvalidControlInput("max_new_per_round must be an int >= 1")


@dataclass(frozen=True)
class WorkRequest:
    """Scheduled question generation work request for a single bucket."""

    bucket: str
    count: int                      # >= 1

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, str) or not self.bucket.strip():
            raise InvalidControlInput("bucket must be a non-empty string")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise InvalidControlInput("count must be an int >= 1")


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of quota deficit evaluation and work scheduling."""

    status: Literal["complete", "schedule", "needs_review"]
    requests: tuple[WorkRequest, ...]      # empty unless status == "schedule"
    reasons: tuple[str, ...]
    rounds_used_after: int
    rule_version: str

    def __post_init__(self) -> None:
        if self.status not in ("complete", "schedule", "needs_review"):
            raise InvalidControlInput(f"invalid status: {self.status!r}")
        if not isinstance(self.requests, tuple):
            raise InvalidControlInput("requests must be a tuple")
        if any(not isinstance(r, WorkRequest) for r in self.requests):
            raise InvalidControlInput("requests elements must be WorkRequest instances")
        if self.status != "schedule" and self.requests:
            raise InvalidControlInput("requests must be empty unless status is 'schedule'")
        if not isinstance(self.reasons, tuple):
            raise InvalidControlInput("reasons must be a tuple")
        if any(not isinstance(r, str) for r in self.reasons):
            raise InvalidControlInput("reasons elements must be strings")
        if isinstance(self.rounds_used_after, bool) or not isinstance(self.rounds_used_after, int) or self.rounds_used_after < 0:
            raise InvalidControlInput("rounds_used_after must be a non-negative int")
        if not isinstance(self.rule_version, str) or not self.rule_version:
            raise InvalidControlInput("rule_version must be a non-empty string")


def check_quotas(
    policy: QuotaPolicy,
    deficits: Sequence[BucketDeficit],
    rounds_used: int,
) -> QuotaDecision:
    """Check bucket deficits and schedule new question generation rounds.

    Existing qualified questions are never part of the input and are never touched:
    the result only ever ADDS work.
    """
    if not isinstance(policy, QuotaPolicy):
        raise InvalidControlInput("policy must be a QuotaPolicy")
    if not isinstance(deficits, Sequence) or isinstance(deficits, (str, bytes)):
        raise InvalidControlInput("deficits must be a sequence of BucketDeficit")
    for item in deficits:
        if not isinstance(item, BucketDeficit):
            raise InvalidControlInput("deficits items must be BucketDeficit")
    if isinstance(rounds_used, bool) or not isinstance(rounds_used, int) or rounds_used < 0:
        raise InvalidControlInput("rounds_used must be a non-negative int")

    bucket_names = [d.bucket for d in deficits]
    if len(bucket_names) != len(set(bucket_names)):
        raise InvalidControlInput("duplicate bucket in deficits")

    total = sum(d.deficit for d in deficits)
    if total == 0:
        return QuotaDecision(
            status="complete",
            requests=(),
            reasons=(),
            rounds_used_after=rounds_used,
            rule_version=CONTROL_RULE_VERSION,
        )

    if rounds_used >= policy.max_rounds:
        return QuotaDecision(
            status="needs_review",
            requests=(),
            reasons=("quota_round_limit",),
            rounds_used_after=rounds_used,
            rule_version=CONTROL_RULE_VERSION,
        )

    rem: dict[str, int] = {d.bucket: d.deficit for d in deficits}
    granted: dict[str, int] = {d.bucket: 0 for d in deficits}

    for _ in range(policy.max_new_per_round):
        available = [b for b, def_val in rem.items() if def_val > 0]
        if not available:
            break
        chosen = min(available, key=lambda b: (-rem[b], b))
        rem[chosen] -= 1
        granted[chosen] += 1

    requests = tuple(
        WorkRequest(bucket=b, count=granted[b])
        for b in sorted(granted.keys())
        if granted[b] > 0
    )
    scheduled_count = sum(r.count for r in requests)
    reasons = ("deficit_remaining",) if scheduled_count < total else ()

    return QuotaDecision(
        status="schedule",
        requests=requests,
        reasons=reasons,
        rounds_used_after=rounds_used + 1,
        rule_version=CONTROL_RULE_VERSION,
    )


__all__ = [
    "CONTROL_RULE_VERSION",
    "NON_REGENERABLE",
    "InvalidControlInput",
    "Policy",
    "AttemptState",
    "Outcome",
    "Decision",
    "classify_candidate",
    "BucketDeficit",
    "QuotaPolicy",
    "WorkRequest",
    "QuotaDecision",
    "check_quotas",
]
