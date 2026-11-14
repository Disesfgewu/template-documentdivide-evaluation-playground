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
from . import run_estimate as est

router = APIRouter()
BUILD_ACTION = "build_edit_freeze"


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1, max_length=128)
    # Optional (Issue #186): without it the cap is DERIVED from the worst case of exactly the calls this run will make, so the owner
    # never types a price. Scripts may still pass an explicit cap.
    budget_cap_micros: Optional[int] = Field(default=None, gt=0, le=1_000_000_000_000)
    # Generation only: HOW MANY questions to generate (default = the plan's total, at most the plan's total).
    question_count: Optional[int] = Field(default=None, ge=1, le=1000)
    max_output_tokens: int = Field(default=MAX_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)
    currency: str = Field(default="USD", min_length=1, max_length=16)
    pricing_revision: str = Field(default="configured-prices", min_length=1, max_length=128)


def _derive_or_explicit(conn, settings, actor_id, project_id, benchmark_id, page_count, body: GenerateRequest, phase: str):
    """(cap for THIS run, requested split or None, estimate or None). An explicit cap is used as given; otherwise the cap is the worst case
    of the calls of this run. Estimation problems (no prices, over the ceiling, a bad question_count) are clean 4xx answers."""
    if phase != "generate" and body.question_count is not None:
        raise ApiError(400, "bad_request", "question_count only applies to generation")
    try:
        if phase == "generate":
            count = body.question_count if body.question_count is not None else est.default_question_count(page_count)
            if body.budget_cap_micros is not None:                          # explicit cap: still validate the count and record the split
                requested = est.split_question_count(page_count, count) if body.question_count is not None else None
                return body.budget_cap_micros, requested, None
            info, requested = est.estimate_generate(conn, settings, project_id, benchmark_id, page_count, count, body.max_output_tokens)
            return info["estimated_max_micros"], requested, info
        if body.budget_cap_micros is not None:
            return body.budget_cap_micros, None, None
        info = est.estimate_validate(conn, settings, project_id, actor_id, benchmark_id, body.max_output_tokens)
        return info["estimated_max_micros"], None, info
    except est.EstimateError as exc:
        raise ApiError(409 if "ceiling" in str(exc) or "prices" in str(exc) else 400, "conflict" if "ceiling" in str(exc) or "prices" in str(exc)
                       else "bad_request", str(exc)) from None


def _start(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
           body: GenerateRequest, phase: str = "generate", settings=None) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
    benchmark_id, _, _, page_count = _find(conn, project_id, logical_id, revision)
    run_cap, requested, info = _derive_or_explicit(conn, settings, actor_id, project_id, benchmark_id, page_count, body, phase)
    if info is not None and info["calls"] == 0:
        raise ApiError(409, "conflict", "there is nothing to send: no call would be made for this request")
    # run_cap is the budget for THIS run: the stored per-benchmark cap is what the benchmark has already spent
    # or holds plus that amount, so a validation run is not refused because the generation run spent money earlier.
    used = benchmark_budget(conn, project_id, benchmark_id, 0)
    total_cap = used.spent + used.held + run_cap
    try:
        job_id = enqueue_generate_job(
            conn, project_id=project_id, actor_id=actor_id, benchmark_id=benchmark_id, model=body.model,
            max_output_tokens=body.max_output_tokens, budget_cap_micros=total_cap, currency=body.currency,
            pricing_revision=body.pricing_revision, phase=phase, requested=requested)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except GenerateJobActive:
        raise ApiError(409, "conflict", "a generation job is already active for this benchmark") from None
    except GenerationError as exc:
        raise ApiError(409, "conflict", f"generation cannot start: {exc}") from None
    out = {"job_id": str(job_id), "benchmark_revision_id": str(benchmark_id), "status": "queued", "phase": phase,
           "benchmark_status": "generating" if phase == "generate" else "validating", "model": body.model, "max_output_tokens": body.max_output_tokens,
           "budget_cap_micros": run_cap, "budget_derived": info is not None, "benchmark_cap_micros": total_cap,
           "general_target": general_target(page_count), "cross_page_target": cross_page_target(page_count),
           "confusable_target": confusable_target(page_count)}
    if info is not None:
        out["estimate"] = info
    if requested is not None:
        out["requested"] = {"general": requested[0], "cross_page": requested[1], "confusable": requested[2]}
    return out


def _estimate(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int, phase: str,
              question_count: Optional[int], settings, run_id: Optional[uuid.UUID] = None) -> dict:
    """What the next run of ``phase`` (generate / validate / answer / judge of ``run_id``) can cost at most, before it starts."""
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
    benchmark_id, _, _, page_count = _find(conn, project_id, logical_id, revision)
    try:
        if phase == "generate":
            count = question_count if question_count is not None else est.default_question_count(page_count)
            return est.estimate_generate(conn, settings, project_id, benchmark_id, page_count, count)[0]
        if question_count is not None:
            raise ApiError(400, "bad_request", "question_count only applies to generation")
        if phase == "validate":
            return est.estimate_validate(conn, settings, project_id, actor_id, benchmark_id)
        if phase == "answer":
            return est.estimate_answer(conn, settings, project_id, benchmark_id)
        if phase == "judge":
            if run_id is None or conn.execute("SELECT 1 FROM runs WHERE id = %s AND project_id = %s AND benchmark_id = %s",
                                              (run_id, project_id, benchmark_id)).fetchone() is None:
                raise ApiError(404, "not_found", "run not found")
            return est.estimate_judge(conn, settings, project_id, benchmark_id, run_id)
    except est.EstimateError as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    raise ApiError(400, "bad_request", "phase must be generate, validate, answer or judge")


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
        counts = dict(conn.execute("SELECT quota_bucket, count(*) FROM active_questions WHERE benchmark_id = %s GROUP BY 1",
                                   (benchmark_id,)).fetchall())
        out["questions"] = sum(int(v) for v in counts.values())          # the questions that would be frozen (not excluded)
        out["excluded"] = conn.execute("SELECT (SELECT count(*) FROM questions WHERE benchmark_id = %s) - %s", (benchmark_id, out["questions"])).fetchone()[0]
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
            "WHERE benchmark_id = %s AND question_id IN (SELECT id FROM active_questions WHERE benchmark_id = %s) "
            "ORDER BY question_id, created_at DESC, id DESC) v GROUP BY verdict", (benchmark_id, benchmark_id)).fetchall())
        validated = sum(int(n) for n in verdicts.values())
        out["validation"] = {"pass": int(verdicts.get("pass", 0)), "fail": int(verdicts.get("fail", 0)),
                             "needs_review": int(verdicts.get("needs_review", 0)),
                             "unvalidated": max(0, out["questions"] - validated),
                             "note": "automated only; the validator is the same provider and model as the generator; human review is skipped"}
        out["job"] = {"job_id": str(job_id), "phase": phase, "status": job_status, "attempt": attempt, "model": model,
                      "max_output_tokens": cap_tokens, "currency": currency, "pricing_revision": pricing}
        if job_status == "failed":      # WHY it failed (the fixed code the worker finished the job with), so the page can say it
            why = conn.execute("SELECT reason FROM job_transitions WHERE job_id = %s AND to_status::text = 'failed' ORDER BY id DESC LIMIT 1",
                               (job_id,)).fetchone()
            out["job"]["failure_reason"] = why[0] if why else None
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
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body, "generate", request.app.state.settings))


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
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body, "validate", request.app.state.settings))


async def estimate_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    try:
        run_id = uuid.UUID(request.query_params["run_id"]) if "run_id" in request.query_params else None
    except ValueError:
        raise ApiError(400, "bad_request", "run_id must be a UUID") from None
    raw_count = request.query_params.get("question_count")
    if raw_count is not None and (not raw_count.isascii() or not raw_count.isdigit() or not 1 <= int(raw_count) <= 1000):
        raise ApiError(400, "bad_request", "question_count must be an integer 1..1000")
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _estimate, conn, actor_id, project_id, logical_id, revision, request.query_params.get("phase", "generate"),
        int(raw_count) if raw_count is not None else None, request.app.state.settings, run_id))


async def progress_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _progress, conn, actor_id, project_id, logical_id, revision))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/generate", start_handler, methods=["POST"],
                     name="start_generation_v1", status_code=202)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/validate", validate_handler, methods=["POST"],
                     name="start_validation_v1", status_code=202)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/estimate", estimate_handler, methods=["GET"],
                     name="estimate_run_cost_v1")
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/generate", progress_handler, methods=["GET"],
                     name="get_generation_progress_v1")
