"""Generation jobs and the LLM call ledger (P-D slice 2, Refs #7, Refs #10).

``enqueue_generate_job`` queues one ``document.generate`` job for a benchmark in ``planning`` that already pins a parse
(P-A), and moves the benchmark to ``generating`` in the same transaction. ``record_llm_calls`` writes the append-only
``llm_calls`` rows (tokens, cost, outcome, sha256 + length; never the key, a prompt or the reply text).

Budget (fail-closed): ``spent`` is the sum of known charges and ``held`` the worst-case reservations of calls whose cost
is unknown; ``remaining_budget`` = cap - spent - held, so an unknown cost is never counted as free."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import uuid
from typing import Iterable, Optional, Sequence

import psycopg

from lab_db.jobs import ACTIVE_JOB_STATUSES
from lab_db.repo import NotFound, PermissionDenied, authorize, transition_benchmark

__all__ = ["BudgetState", "GenerateJobActive", "GenerationError", "MAX_OUTPUT_TOKENS", "benchmark_budget",
           "enqueue_generate_job", "record_llm_calls", "request_sha256", "reserve_batch", "worst_case_micros"]

MAX_OUTPUT_TOKENS = 500          # the owner's hard cap per call (Issue #7, D7); the table checks it too
DEFAULT_GENERATE_MAX_ATTEMPTS = 3


class GenerationError(Exception):
    """The job cannot be queued (fixed, value-free message)."""


class GenerateJobActive(GenerationError):
    def __init__(self, job_id: uuid.UUID):
        super().__init__("a_generate_job_is_already_active")
        self.job_id = job_id


@dataclass(frozen=True)
class BudgetState:
    cap: int
    spent: int
    held: int

    @property
    def remaining(self) -> int:
        return self.cap - self.spent - self.held


def worst_case_micros(prompt_bytes: int, max_output_tokens: int, input_micros_per_mtok: int,
                      output_micros_per_mtok: int) -> int:
    """The same bound lab-gen reserves: every prompt byte is at least one token, the reply uses the whole cap;
    rounded up once. Used when a batch's result is lost and its cost has to be held, not guessed as zero."""
    if min(prompt_bytes, max_output_tokens, input_micros_per_mtok, output_micros_per_mtok) < 0:
        raise ValueError("negative usage or price")
    total = prompt_bytes * input_micros_per_mtok + max_output_tokens * output_micros_per_mtok
    return max(1, -(-total // 1_000_000))


def request_sha256(model: str, system: str, user: str) -> str:
    """Identical to lab-gen's request hash: sha256(model NUL system NUL user)."""
    return hashlib.sha256(f"{model}\x00{system}\x00{user}".encode("utf-8")).hexdigest()


def enqueue_generate_job(conn: psycopg.Connection, *, project_id: uuid.UUID, actor_id: uuid.UUID,
                         benchmark_id: uuid.UUID, model: str, max_output_tokens: int, budget_cap_micros: int,
                         currency: str, pricing_revision: str,
                         max_attempts: int = DEFAULT_GENERATE_MAX_ATTEMPTS) -> uuid.UUID:
    """Queue a generation job and move the benchmark ``planning -> generating``.

    Authorized here for ``build_edit_freeze`` (owner/maintainer; non-member ``NotFound``, other roles
    ``PermissionDenied``). The cap above 500 output tokens is refused before anything is written."""
    if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise GenerationError("max_output_tokens_out_of_range")
    if budget_cap_micros <= 0:
        raise GenerationError("budget_cap_must_be_positive")
    job_id, op_id = uuid.uuid4(), uuid.uuid4()
    with conn.transaction():
        decision = authorize(conn, project_id, actor_id, "build_edit_freeze")
        if decision.condition is not None:
            raise PermissionDenied(f"{decision.role} may not start generation")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"generate_job:{project_id}:{benchmark_id}",))
        bench = conn.execute("SELECT status::text, row_version FROM benchmark_revisions WHERE project_id = %s AND id = %s "
                             "FOR UPDATE", (project_id, benchmark_id)).fetchone()
        if bench is None:
            raise NotFound("benchmark")
        if bench[0] != "planning":
            raise GenerationError("benchmark_is_not_in_planning")
        if conn.execute("SELECT 1 FROM benchmark_parse_sources WHERE benchmark_id = %s", (benchmark_id,)).fetchone() is None:
            raise GenerationError("benchmark_has_no_pinned_parse")
        active = conn.execute(
            "SELECT j.id FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
            "WHERE g.project_id = %s AND g.benchmark_id = %s AND j.status::text = ANY(%s) LIMIT 1",
            (project_id, benchmark_id, list(ACTIVE_JOB_STATUSES))).fetchone()
        if active is not None:
            raise GenerateJobActive(active[0])
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'document.generate', %s, 'succeeded', now())", (op_id, project_id, actor_id))
        conn.execute("INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) "
                     "VALUES (%s, %s, 'document.generate', 'queued', %s, true)", (job_id, project_id, max_attempts))
        conn.execute(
            "INSERT INTO generate_jobs (job_id, project_id, benchmark_id, operation_id, requested_by, model, "
            "max_output_tokens, budget_cap_micros, currency, pricing_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (job_id, project_id, benchmark_id, op_id, actor_id, model, max_output_tokens, budget_cap_micros, currency,
             pricing_revision))
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, outcome, operation_id) "
            "VALUES (%s, %s, %s, 'generate.requested', 'generate_job', %s, 'succeeded', %s)",
            (uuid.uuid4(), project_id, actor_id, job_id, op_id))
        transition_benchmark(conn, project_id, benchmark_id, bench[1], "generating", "generation job queued", actor_id)
    return job_id


def benchmark_budget(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, cap: int) -> BudgetState:
    """Spend and holds from the CURRENT row (latest seq) of every call of every attempt of the benchmark's jobs."""
    spent, held = conn.execute(
        "SELECT COALESCE(sum(GREATEST(charged_micros, 0)), 0), COALESCE(sum(held_micros), 0) FROM ("
        "SELECT DISTINCT ON (job_id, attempt, call_id) charged_micros, held_micros FROM llm_calls "
        "WHERE project_id = %s AND benchmark_id = %s ORDER BY job_id, attempt, call_id, seq DESC) s",
        (project_id, benchmark_id)).fetchone()
    return BudgetState(cap=cap, spent=int(spent), held=int(held))


_COLUMNS = ("id, project_id, benchmark_id, job_id, call_id, attempt, model, status, reason, usage_known, input_tokens, "
            "output_tokens, charged_micros, held_micros, attempts, currency, pricing_revision, request_sha256, "
            "response_length, response_sha256")


def record_llm_calls(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, job_id: uuid.UUID,
                     attempt: int, model: str, currency: str, pricing_revision: str, calls: Iterable[dict]) -> int:
    """Append one ``llm_calls`` row per call. Idempotent only for a replay of the SAME attempt (the unique indexes are
    per (job, attempt, call) and per kind: reservation or settlement); a retry is a new attempt and always inserts.
    Returns the rows written. Each call dict: call_id, status, reason, usage_known, input_tokens, output_tokens,
    charged_micros, held_micros, attempts, request_sha256, response_length, response_sha256. The database checks the
    cost/usage invariants."""
    written = 0
    for c in calls:
        target = "WHERE status = 'reserved'" if c["status"] == "reserved" else "WHERE status <> 'reserved'"
        cur = conn.execute(
            f"INSERT INTO llm_calls ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (job_id, attempt, call_id) {target} DO NOTHING",
            (uuid.uuid4(), project_id, benchmark_id, job_id, c["call_id"], attempt, model, c["status"],
             (c.get("reason") or None), c["usage_known"], c["input_tokens"], c["output_tokens"], c["charged_micros"],
             c["held_micros"], c["attempts"], currency, pricing_revision, c["request_sha256"], c["response_length"],
             c.get("response_sha256")))
        written += cur.rowcount
    return written


def _row(call_id: str, status: str, reason: str, held: int, request_hash: str) -> dict:
    return {"call_id": call_id, "status": status, "reason": reason, "usage_known": False, "input_tokens": 0,
            "output_tokens": 0, "charged_micros": -1, "held_micros": held, "attempts": 0, "request_sha256": request_hash,
            "response_length": 0, "response_sha256": None}


def reserve_batch(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, job_id: uuid.UUID,
                  attempt: int, cap: int, model: str, currency: str, pricing_revision: str,
                  calls: Sequence[tuple[str, str, int]]) -> tuple[list[str], int]:
    """Reserve the worst case of each call BEFORE anything is spawned. ``calls`` = (call_id, request_sha256, worst).

    One transaction under a per-benchmark advisory lock (two workers can never both spend the same remaining budget):
    in order, a call whose worst case fits the remaining budget gets a ``reserved`` row (held = worst); the first call
    that does not fit, and every later one, gets a ``skipped_budget`` row and is never sent (fail-closed).
    Returns (call ids that may be sent, their total hold)."""
    sendable: list[str] = []
    total = 0
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"llm_budget:{project_id}:{benchmark_id}",))
        remaining = benchmark_budget(conn, project_id, benchmark_id, cap).remaining
        stopped = False
        for call_id, request_hash, worst in calls:
            if not stopped and worst <= remaining:
                record_llm_calls(conn, project_id, benchmark_id, job_id, attempt, model, currency, pricing_revision,
                                 [_row(call_id, "reserved", "reserved before spawn", worst, request_hash)])
                remaining -= worst
                total += worst
                sendable.append(call_id)
            else:
                stopped = True
                record_llm_calls(conn, project_id, benchmark_id, job_id, attempt, model, currency, pricing_revision,
                                 [_row(call_id, "skipped_budget", "worst case does not fit the remaining budget", 0, request_hash)])
    return sendable, total
