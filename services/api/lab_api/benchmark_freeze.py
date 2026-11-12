"""Freeze a benchmark revision, owner only, honestly partial (PM-DECISION 2026-11-12 item 2, Refs #10).

``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/freeze`` with the body ``{"compliance": "partial"}``
(the caller must say the word: the freeze is NOT a full-compliance release):

* only the project OWNER (SPEC 5.1/5.3); maintainers, reviewers and participants get 403, non-members 404;
* the benchmark must be in ``validating`` and EVERY question's latest automated validation must be ``pass``. A question
  with a fail, needs_review or no validation BLOCKS the freeze (409 with the counts): nothing is excluded or deleted, because a
  frozen set is immutable and children are never removed. The owner has to resolve it (validate again, or start a new
  revision) before freezing;
* compliance is ``partial``: human review was skipped by the owner's scope (D6) and the quota buckets are incomplete
  (chart_table and confusable are not generated, general/cross_page may be short); ``compliant`` is refused.
The database gate (migration 0017) enforces the same rules for every writer; this endpoint reports them as clean 409/403s.
The response lists what is missing against N and Q and states that the verdicts are automated by the generator's own model."""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import MAX_BODY_BYTES, _find
from lab_db.jobs import ACTIVE_JOB_STATUSES
from lab_db.repo import FreezeIneligible, NotFound, PermissionDenied, StaleVersion, authorize, transition_benchmark

router = APIRouter()
BUILD_ACTION = "build_edit_freeze"
LIMITS = [
    "Compliance is PARTIAL: human review was skipped (D6) and the quota buckets are incomplete.",
    "Validation verdicts are automated and come from the same provider and model as the generator; they are not independent.",
    "A question answerable without the page is not detected; 'necessary for the question' is not proven by an ablation.",
]


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    compliance: str = Field(pattern="^partial$")


def _freeze(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    try:
        decision = authorize(conn, project_id, actor_id, BUILD_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if decision.role != "owner":
        raise ApiError(403, "permission_denied", f"{decision.role} may not freeze a benchmark: only the project owner may")
    try:
        with conn.transaction():
            benchmark_id, _, status, page_count = _find(conn, project_id, logical_id, revision, lock=True)
            if status != "validating":
                raise ApiError(409, "conflict", f"a benchmark in status {status} cannot be frozen (it must be 'validating')")
            active = conn.execute(
                "SELECT j.id FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
                "WHERE g.project_id = %s AND g.benchmark_id = %s AND j.status::text = ANY(%s) LIMIT 1",
                (project_id, benchmark_id, list(ACTIVE_JOB_STATUSES))).fetchone()
            if active is not None:     # a re-validation in flight could still change the verdicts (and would hit the frozen guards)
                raise ApiError(409, "conflict", "a generation or validation job is still active for this benchmark; "
                                                "wait for it to finish (or cancel it) before freezing")
            conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))   # question_validations is gold-RLS
            totals = dict(conn.execute("SELECT quota_bucket, count(*) FROM questions WHERE benchmark_id = %s GROUP BY 1",
                                       (benchmark_id,)).fetchall())
            verdicts = dict(conn.execute(
                "SELECT COALESCE(latest.verdict, 'unvalidated'), count(*) FROM questions q LEFT JOIN LATERAL ("
                "SELECT verdict FROM question_validations v WHERE v.question_id = q.id ORDER BY v.created_at DESC, v.id DESC LIMIT 1) latest ON true "
                "WHERE q.benchmark_id = %s GROUP BY 1", (benchmark_id,)).fetchall())
            n_questions = sum(int(v) for v in totals.values())
            counts = {k: int(verdicts.get(k, 0)) for k in ("pass", "fail", "needs_review", "unvalidated")}
            if n_questions == 0:
                raise ApiError(409, "conflict", "the benchmark has no questions to freeze")
            if counts["pass"] != n_questions:
                raise ApiError(409, "conflict", "every question needs a passing automated validation before the freeze; "
                               f"verdicts: {counts}. Nothing is excluded or deleted: resolve them (validate again) first.")
            row = conn.execute("SELECT row_version, n_target, q_min FROM benchmark_revisions WHERE id = %s", (benchmark_id,)).fetchone()
            version, n_target, q_min = row
            version = transition_benchmark(conn, project_id, benchmark_id, version, "ready_to_freeze",
                                           f"owner freeze requested: {n_questions} questions, all with a passing automated validation",
                                           actor_id)
            transition_benchmark(conn, project_id, benchmark_id, version, "frozen",
                                 f"frozen by the owner as PARTIAL: questions={n_questions}/{n_target}; human review skipped; "
                                 "chart_table and confusable not generated", actor_id, compliance="partial")
            frozen_at = conn.execute("SELECT frozen_at FROM benchmark_revisions WHERE id = %s", (benchmark_id,)).fetchone()[0]
    except FreezeIneligible as exc:
        raise ApiError(409, "conflict", str(exc).split("\n")[0]) from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except StaleVersion:
        raise ApiError(409, "conflict", "the benchmark changed while freezing; try again") from None
    by_bucket = {b: int(totals.get(b, 0)) for b in ("general", "cross_page", "chart_table", "confusable")}
    shortfall = {"total": max(0, n_target - n_questions),
                 "general": max(0, (n_target - 3 * q_min) - by_bucket["general"]),
                 **{b: max(0, q_min - by_bucket[b]) for b in ("cross_page", "chart_table", "confusable")}}
    return {"benchmark_revision_id": str(benchmark_id), "logical_id": logical_id, "revision": revision, "status": "frozen",
            "compliance": "partial", "frozen_at": frozen_at.isoformat(), "questions": n_questions, "by_bucket": by_bucket,
            "n_target": n_target, "q_min": q_min, "shortfall": shortfall, "validation": counts,
            "human_review": "skipped", "limits": LIMITS}


async def freeze_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        FreezeRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", 'the body must be {"compliance": "partial"}: a freeze is partial, not a full release') from None
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _freeze, conn, actor_id, project_id, logical_id, revision))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/freeze", freeze_handler, methods=["POST"],
                     name="freeze_benchmark_v1")
