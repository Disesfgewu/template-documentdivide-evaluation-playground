"""What a model run can cost AT MOST, shown before it starts and used to derive the budget cap (Issue #186).

The owner says HOW MANY questions to generate, not a price. The worker reserves, per call, the worst case lab-gen itself uses (every
prompt byte at least one token, the reply uses the whole output cap; ``lab_db.generation.worst_case_micros``) and sends only the calls
that fit the cap. So the cap that fits exactly the requested calls is the sum of those worst cases:
* generation and validation: computed from the REAL prompts these calls will send (the same builders the worker uses);
* answering and judging: their prompts are built from the PDF's retrieval / the answers, so the API cannot build them; a bound is used
  instead (the largest pages the method could retrieve, plus the fixed parts, with a margin). Fail-closed: if a real prompt is ever
  bigger than the bound, that call is skipped as budget exhausted and recorded, never overspent.
A hard ceiling stops a run whose worst case is absurd. Prices come from the same configuration the worker uses; without them nothing
can be derived and the caller must give an explicit cap."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

import psycopg

from lab_db.generation import MAX_OUTPUT_TOKENS, worst_case_micros
from .config import Settings
from .generate_questions import (confusable_target, cross_page_target, general_target, load_clean_pages, make_build_calls)
from . import validate_questions
from .method_answer import MAX_HIT_CHARS, SYSTEM as ANSWER_SYSTEM
from .method_judge import SYSTEM as JUDGE_SYSTEM

CEILING_MICROS = 5_000_000          # 5 USD: one run's worst case above this is refused, whatever the owner asks
TOP_K_HITS = 5                      # the answering method retrieves five pages (answer_run.BUDGET)
BOUND_MARGIN = 1.25                 # pypdf text and the parse's text differ a little
ANSWER_FIXED_BYTES = 4_000          # question, tags and framing around the hits
JUDGE_FIXED_BYTES = 24_000          # gold facts, the answer and up to 12 claims around the cited hits


class EstimateError(Exception):
    """The cost cannot be derived or is over the ceiling (fixed, value-free message)."""


def usd(micros: int) -> str:
    return f"{(Decimal(micros) / Decimal(1_000_000)).quantize(Decimal('0.0001'))}"


def prices(settings: Settings) -> tuple[int, int]:
    if settings.gen_price_in_micros_per_mtok is None or settings.gen_price_out_micros_per_mtok is None:
        raise EstimateError("the model prices are not configured for the API (LAB_GEN_PRICE_IN/OUT_MICROS_PER_MTOK): give an explicit budget")
    return settings.gen_price_in_micros_per_mtok, settings.gen_price_out_micros_per_mtok


def split_question_count(page_count: int, n: int) -> tuple[int, int, int]:
    """(general, cross_page, confusable) for ``n`` questions, in the plan's proportions; ``n`` at most the plan's total."""
    g, x, c = general_target(page_count), cross_page_target(page_count), confusable_target(page_count)
    total = g + x + c
    if not 1 <= n <= total:
        raise EstimateError(f"question_count must be 1..{total} (the plan's total)")
    if n == total:
        return g, x, c
    cross, conf = min(x, round(n * x / total)), min(c, round(n * c / total))
    return n - cross - conf, cross, conf


def default_question_count(page_count: int) -> int:
    return general_target(page_count) + cross_page_target(page_count) + confusable_target(page_count)


def _result(phase: str, calls: int, worst: int, extra: Optional[dict] = None) -> dict:
    if worst > CEILING_MICROS:
        raise EstimateError(f"the worst case is about {usd(worst)} USD, above the ceiling of {usd(CEILING_MICROS)} USD: ask for fewer questions")
    out = {"phase": phase, "calls": calls, "estimated_max_micros": worst, "estimated_max_usd": usd(worst), "currency": "USD",
           "ceiling_usd": usd(CEILING_MICROS), "max_output_tokens": MAX_OUTPUT_TOKENS,
           "basis": "worst case per call: every prompt byte is a token and the reply uses the whole output cap"}
    out.update(extra or {})
    return out


def _worst_of(calls, settings: Settings, max_output_tokens: int) -> int:
    pin, pout = prices(settings)
    return sum(worst_case_micros(len((c.system + c.user).encode("utf-8")), max_output_tokens, pin, pout) for c in calls)


def estimate_generate(conn: psycopg.Connection, settings: Settings, project_id: uuid.UUID, benchmark_id: uuid.UUID, page_count: int,
                      question_count: int, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> tuple[dict, tuple[int, int, int]]:
    requested = split_question_count(page_count, question_count)
    calls = list(make_build_calls()(conn, project_id, benchmark_id, requested))
    split = {"general": requested[0], "cross_page": requested[1], "confusable": requested[2]}
    return _result("generate", len(calls), _worst_of(calls, settings, max_output_tokens),
                   {"question_count": question_count, "split": split, "plan_total": default_question_count(page_count)}), requested


def estimate_validate(conn: psycopg.Connection, settings: Settings, project_id: uuid.UUID, actor_id: uuid.UUID, benchmark_id: uuid.UUID,
                      max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))        # the validation prompts read the gold
        calls = list(validate_questions.make_build_calls()(conn, project_id, benchmark_id))
    return _result("validate", len(calls), _worst_of(calls, settings, max_output_tokens))


def _largest_pages_bytes(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID) -> int:
    _, pages, _ = load_clean_pages(conn, project_id, benchmark_id)
    sizes = sorted((len("\n\n".join(u.normalized_content for u in units)[:MAX_HIT_CHARS].encode("utf-8")) for units in pages.values()),
                   reverse=True)
    return int(sum(sizes[:TOP_K_HITS]) * BOUND_MARGIN)


def _bound_worst(settings: Settings, prompt_bytes: int, max_output_tokens: int) -> int:
    pin, pout = prices(settings)
    return worst_case_micros(prompt_bytes, max_output_tokens, pin, pout)


def estimate_answer(conn: psycopg.Connection, settings: Settings, project_id: uuid.UUID, benchmark_id: uuid.UUID,
                    max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    n = conn.execute("SELECT count(*) FROM active_questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0]
    per_call = _bound_worst(settings, len(ANSWER_SYSTEM.encode("utf-8")) + ANSWER_FIXED_BYTES + _largest_pages_bytes(conn, project_id, benchmark_id),
                            max_output_tokens)
    return _result("answer", int(n), int(n) * per_call, {"bound": True})


def estimate_judge(conn: psycopg.Connection, settings: Settings, project_id: uuid.UUID, benchmark_id: uuid.UUID, run_id: uuid.UUID,
                   max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    n = conn.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND status = 'accepted' AND answer_status = 'answered' "
                     "AND failure_reason IS NULL", (run_id,)).fetchone()[0]
    per_call = _bound_worst(settings, len(JUDGE_SYSTEM.encode("utf-8")) + JUDGE_FIXED_BYTES + _largest_pages_bytes(conn, project_id, benchmark_id),
                            max_output_tokens)
    return _result("judge", int(n), int(n) * per_call, {"bound": True})
