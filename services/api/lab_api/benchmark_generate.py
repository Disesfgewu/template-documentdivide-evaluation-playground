"""Start and watch question generation for a benchmark revision (P-D slice 3b, Refs #7, Refs #10).

* ``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/generate`` queues ONE ``document.generate`` job
  (``enqueue_generate_job``: owner/maintainer only, the benchmark must be in ``planning`` with a pinned parse, the output cap
  is at most 500 tokens, the budget cap must be positive) and moves the benchmark to ``generating``. 202 + job id.
* ``POST .../validate`` (same body and rules, benchmark in ``validating``) queues the automated validation job
  (P-D slice 4); ``GET`` then also reports the verdict counts (never question text).
* ``GET .../generate`` reports progress for roles that may read generation context: the job status, the ledger
  (calls by state, spent, held, remaining), the question count against the general target, and the last generation
  audit counts. It never returns question text, answers or any gold.

The call itself is made later by the generation worker (``python -m lab_api.generate_worker_main``); this API makes none."""
from __future__ import annotations

import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import BENCHMARK_RESOURCE_TYPE, LOGICAL_ID_PATTERN, MAX_BODY_BYTES, READ_ACTION, _authorize, _find
from .generate_questions import confusable_target, cross_page_target, general_target
from lab_db.generation import GenerateJobActive, GenerationError, MAX_OUTPUT_TOKENS, benchmark_budget, enqueue_generate_job
from lab_db.repo import NotFound, PermissionDenied

router = APIRouter()
BUILD_ACTION = "build_edit_freeze"


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1, max_length=128)
    budget_cap_micros: int = Field(gt=0, le=1_000_000_000_000)
    max_output_tokens: int = Field(default=MAX_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)
    currency: str = Field(default="USD", min_length=1, max_length=16)
    pricing_revision: str = Field(default="configured-prices", min_length=1, max_length=128)


def _start(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
           body: GenerateRequest, phase: str = "generate") -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
    benchmark_id, _, _, page_count = _find(conn, project_id, logical_id, revision)
    # budget_cap_micros is the budget for THIS run: the stored per-benchmark cap is what the benchmark has already spent
    # or holds plus that amount, so a validation run is not refused because the generation run spent money earlier.
    used = benchmark_budget(conn, project_id, benchmark_id, 0)
    total_cap = used.spent + used.held + body.budget_cap_micros
    try:
        job_id = enqueue_generate_job(
            conn, project_id=project_id, actor_id=actor_id, benchmark_id=benchmark_id, model=body.model,
            max_output_tokens=body.max_output_tokens, budget_cap_micros=total_cap, currency=body.currency,
            pricing_revision=body.pricing_revision, phase=phase)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except GenerateJobActive:
        raise ApiError(409, "conflict", "a generation job is already active for this benchmark") from None
    except GenerationError as exc:
        raise ApiError(409, "conflict", f"generation cannot start: {exc}") from None
    return {"job_id": str(job_id), "benchmark_revision_id": str(benchmark_id), "status": "queued", "phase": phase,
            "benchmark_status": "generating" if phase == "generate" else "validating", "model": body.model, "max_output_tokens": body.max_output_tokens,
            "budget_cap_micros": body.budget_cap_micros, "benchmark_cap_micros": total_cap,
            "general_target": general_target(page_count), "cross_page_target": cross_page_target(page_count),
            "confusable_target": confusable_target(page_count)}


def _progress(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, READ_ACTION, missing)
    benchmark_id, _, status, page_count = _find(conn, project_id, logical_id, revision)
    with conn.transaction():
        # question_validations is under gold row level security: read it as the (already authorized) caller.
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
        job = conn.execute(
            "SELECT g.job_id, j.status::text, j.attempt, g.model, g.max_output_tokens, g.budget_cap_micros, g.currency, "
            "g.pricing_revision, g.phase FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
            "WHERE g.project_id = %s AND g.benchmark_id = %s ORDER BY g.created_at DESC LIMIT 1", (project_id, benchmark_id)).fetchone()
        out = {"benchmark_revision_id": str(benchmark_id), "logical_id": logical_id, "revision": revision,
               "benchmark_status": status, "general_target": general_target(page_count),
               "cross_page_target": cross_page_target(page_count), "confusable_target": confusable_target(page_count),
               "human_review": "skipped", "limits": ["The general, cross_page and confusable buckets are generated; chart_table is "
                                                     "not guessed (no material proof in this parse).",
                                                     "A question answerable without the page is not detected yet."]}
        counts = dict(conn.execute("SELECT quota_bucket, count(*) FROM questions WHERE benchmark_id = %s GROUP BY 1",
                                   (benchmark_id,)).fetchall())
        out["questions"] = sum(int(v) for v in counts.values())
        out["by_bucket"] = {"general": int(counts.get("general", 0)), "cross_page": int(counts.get("cross_page", 0)),
                            "confusable": int(counts.get("confusable", 0))}
        out["shortfall"] = (max(0, out["general_target"] - out["by_bucket"]["general"])
                            + max(0, out["cross_page_target"] - out["by_bucket"]["cross_page"])
                            + max(0, out["confusable_target"] - out["by_bucket"]["confusable"]))
        if job is None:
            out["job"] = None
            return out
        job_id, job_status, attempt, model, cap_tokens, cap_micros, currency, pricing, phase = job
        state = conn.execute(
            "SELECT status, count(*), COALESCE(sum(GREATEST(charged_micros, 0)), 0), COALESCE(sum(held_micros), 0) FROM ("
            "SELECT DISTINCT ON (job_id, attempt, call_id) status, charged_micros, held_micros FROM llm_calls "
            "WHERE project_id = %s AND benchmark_id = %s ORDER BY job_id, attempt, call_id, seq DESC) s GROUP BY status",
            (project_id, benchmark_id)).fetchall()
        budget = benchmark_budget(conn, project_id, benchmark_id, cap_micros)
        last = conn.execute(
            "SELECT a.reason FROM audit_events a JOIN benchmark_revisions b ON b.resource_id = a.resource_id "
            "WHERE a.project_id = %s AND b.id = %s AND a.action = 'benchmark.questions_generated' "
            "ORDER BY a.occurred_at DESC LIMIT 1", (project_id, benchmark_id)).fetchone()
        verdicts = dict(conn.execute(
            "SELECT verdict, count(*) FROM (SELECT DISTINCT ON (question_id) verdict FROM question_validations "
            "WHERE benchmark_id = %s ORDER BY question_id, created_at DESC, id DESC) v GROUP BY verdict", (benchmark_id,)).fetchall())
        validated = sum(int(n) for n in verdicts.values())
        out["validation"] = {"pass": int(verdicts.get("pass", 0)), "fail": int(verdicts.get("fail", 0)),
                             "needs_review": int(verdicts.get("needs_review", 0)),
                             "unvalidated": max(0, out["questions"] - validated),
                             "note": "automated only; the validator is the same provider and model as the generator; human review is skipped"}
        out["job"] = {"job_id": str(job_id), "phase": phase, "status": job_status, "attempt": attempt, "model": model,
                      "max_output_tokens": cap_tokens, "currency": currency, "pricing_revision": pricing}
        out["ledger"] = {"calls_by_state": {s: int(n) for s, n, _, _ in state}, "spent_micros": budget.spent,
                         "held_micros": budget.held, "cap_micros": budget.cap, "remaining_micros": budget.remaining}
        out["last_generation"] = last[0] if last else None
        return out


async def start_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                        actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:      # refuse before reading the body
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        body = GenerateRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", "invalid request body") from None
    return JSONResponse(status_code=202, content=await _run_blocking(
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body))


async def validate_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        body = GenerateRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", "invalid request body") from None
    return JSONResponse(status_code=202, content=await _run_blocking(
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body, "validate"))


async def progress_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _progress, conn, actor_id, project_id, logical_id, revision))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/generate", start_handler, methods=["POST"],
                     name="start_generation_v1", status_code=202)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/validate", validate_handler, methods=["POST"],
                     name="start_validation_v1", status_code=202)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/generate", progress_handler, methods=["GET"],
                     name="get_generation_progress_v1")
