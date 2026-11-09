"""Answer selection and run comparability (SPEC 16.5, 7.3; T45, T46, R12).

A run scores, per question, the first valid terminal submission: answered or
abstained, in attempt order. Later attempts never replace it, even if they
would score higher; changing an answer needs a new run. Attempts exist only
to repair transport failures or outputs that were not accepted, and those
repairs are bounded."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

TERMINAL = ("answered", "abstained")
# Outcomes that carry no accepted output and may be repaired by a new attempt.
REPAIRABLE = ("transport_failure", "rejected_output")
# Outcomes that end the question for this run without an accepted output.
FINAL_FAILURES = ("timeout", "missing")
Outcome = Literal["answered", "abstained", "transport_failure", "rejected_output", "timeout", "missing"]
OUTCOMES = TERMINAL + REPAIRABLE + FINAL_FAILURES


@dataclass(frozen=True)
class Attempt:
    attempt_number: int   # 1..n, unique per (run, question)
    outcome: Outcome
    submission_id: Optional[str] = None  # set exactly for terminal outcomes


@dataclass(frozen=True)
class Selection:
    """Which submission the run scores for one question.

    ``submission_id`` is None when nothing valid was accepted. The question
    then stays in every denominator as a method failure (scored 0), with
    ``failure_reason`` saying why. ``ignored`` lists attempts made after the
    selected one. They are kept for audit and never scored."""

    submission_id: Optional[str]
    selected_attempt: Optional[int]
    failure_reason: Optional[str]
    ignored: tuple[int, ...]
    retries_exceeded: bool


def select_answer(attempts: Sequence[Attempt], max_repairs: int) -> Selection:
    """First valid terminal answer, with at most ``max_repairs`` repair
    attempts after the first one (SPEC 16.5, T46)."""
    if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or max_repairs < 0:
        raise ValueError("max_repairs must be a non-negative int")
    nums = [a.attempt_number for a in attempts]
    if sorted(nums) != list(range(1, len(nums) + 1)):
        raise ValueError("attempt numbers must be unique and contiguous 1..n")
    ordered = sorted(attempts, key=lambda a: a.attempt_number)
    for a in ordered:
        if a.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {a.outcome!r}")
        if (a.outcome in TERMINAL) != (a.submission_id is not None):
            raise ValueError(f"attempt {a.attempt_number}: submission_id is required exactly for answered/abstained")

    if not ordered:
        return Selection(None, None, "missing", (), False)
    allowed = 1 + max_repairs
    for idx, a in enumerate(ordered):
        if idx >= allowed:
            # A repair beyond the bound is not used, even if it succeeded.
            last = ordered[idx - 1]
            return Selection(None, None, _failure(last), tuple(x.attempt_number for x in ordered[idx:]), True)
        if a.outcome in TERMINAL:
            return Selection(a.submission_id, a.attempt_number, None,
                             tuple(x.attempt_number for x in ordered[idx + 1:]), False)
        if a.outcome in FINAL_FAILURES:
            return Selection(None, None, a.outcome, tuple(x.attempt_number for x in ordered[idx + 1:]), False)
    return Selection(None, None, _failure(ordered[-1]), (), False)


def _failure(a: Attempt) -> str:
    return {"transport_failure": "transport_failure", "rejected_output": "format_error"}.get(a.outcome, a.outcome)


# Settings that must be equal for two runs to be ranked together (SPEC 16.5).
COMPARISON_KEYS = ("benchmark_revision", "question_subset_hash", "grader_revision",
                   "budget_profile", "observation_profile")


@dataclass(frozen=True)
class RunProfile:
    run_id: str
    benchmark_revision: str
    question_subset_hash: str
    grader_revision: str
    budget_profile: str
    observation_profile: str
    answerer_model: Optional[str]  # actual model used; None/blank = not disclosed


@dataclass(frozen=True)
class Comparability:
    rankable: bool
    differences: tuple[str, ...]   # sorted keys that differ; shown to users


def _disclosed(model: Optional[str]) -> bool:
    """A model is disclosed only by a non-blank string; None, '' and
    whitespace-only values all count as undisclosed (T45, SPEC 16.5)."""
    return isinstance(model, str) and model.strip() != ""


def compare_runs(runs: Sequence[RunProfile],
                 claim: Literal["full_system", "retrieval_only"]) -> Comparability:
    """Runs may share one ranking only if every fixed setting is equal (T45).
    A retrieval-only claim also fixes the answerer, and a full-system
    comparison requires each actual answerer to be disclosed."""
    if claim not in ("full_system", "retrieval_only"):
        raise ValueError(f"unknown claim {claim!r}")
    if len(runs) < 2:
        raise ValueError("a comparison needs at least two runs")
    if len({r.run_id for r in runs}) != len(runs):
        raise ValueError("duplicate run_id")
    diffs = {k for k in COMPARISON_KEYS if len({getattr(r, k) for r in runs}) > 1}
    if not all(_disclosed(r.answerer_model) for r in runs):
        diffs.add("answerer_model_undisclosed")
    if claim == "retrieval_only" and len({r.answerer_model for r in runs}) > 1:
        diffs.add("answerer_model")
    ordered = tuple(sorted(diffs))
    return Comparability(not ordered, ordered)
