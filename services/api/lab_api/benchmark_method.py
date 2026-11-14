"""Start a run of the answering method on a frozen benchmark (PM-DECISION on Issue #7, item (a), Refs #12).

``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs`` (owner/maintainer; the benchmark must be frozen; body as
for generation: model, budget_cap_micros [this run's budget], max_output_tokens <= 500): creates the method revision, the run and
an ``answer`` job and returns 202. The API makes no model call: the generation worker runs the job. Read the run with
``GET /api/v1/runs/{id}`` and its scores with ``GET /api/v1/runs/{id}/results`` (``submissions`` counts answered / model
abstentions / method failures separately). A second concurrent method run on the same benchmark is refused (409)."""
from __future__ import annotations

import uuid

import psycopg
import psycopg.errors
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import MAX_BODY_BYTES, _find
from .benchmark_generate import GenerateRequest
from .answer_run import AnswerRunError, start_answer_run
from .judge_run import JudgeRunError, start_judge_run
from . import run_estimate as est
from lab_db.repo import NotFound, PermissionDenied

router = APIRouter()


def _cap(body: GenerateRequest, derive) -> tuple[int, dict | None]:
    """The explicit cap, or the derived one (``derive()`` returns an estimate); question_count does not apply to these runs."""
    if body.question_count is not None:
        raise ApiError(400, "bad_request", "question_count only applies to generation")
    if body.budget_cap_micros is not None:
        return body.budget_cap_micros, None
    try:
        info = derive()
    except est.EstimateError as exc:
        raise ApiError(409, "conflict", str(exc)) from None
    if info["calls"] == 0:
        raise ApiError(409, "conflict", "there is nothing to send: no call would be made for this request")
    return info["estimated_max_micros"], info


def _start(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
           body: GenerateRequest, settings=None) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    try:
        benchmark_id, _, _, _ = _find(conn, project_id, logical_id, revision)
        cap, info = _cap(body, lambda: est.estimate_answer(conn, settings, project_id, benchmark_id, body.max_output_tokens))
        out = start_answer_run(conn, project_id, benchmark_id, actor_id, model=body.model, budget_cap_micros=cap,
                               currency=body.currency, pricing_revision=body.pricing_revision,
                               max_output_tokens=body.max_output_tokens)
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "conflict", "another method run is registering the same method revision; try again") from None
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except AnswerRunError as exc:
        raise ApiError(409, "conflict", f"the method run cannot start: {exc}") from None
    out["results_url"] = f"/api/v1/runs/{out['run_id']}/results"
    out["budget_derived"] = info is not None
    if info is not None:
        out["estimate"] = info
    return out


async def start_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
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
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body, request.app.state.settings))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs", start_handler, methods=["POST"],
                     name="start_method_run_v1", status_code=202)


def _start_judge(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, body: GenerateRequest,
                 settings=None) -> dict:
    try:
        row = conn.execute("SELECT benchmark_id FROM runs WHERE id = %s AND project_id = %s", (run_id, project_id)).fetchone()
        if row is None:
            raise NotFound("run")
        cap, info = _cap(body, lambda: est.estimate_judge(conn, settings, project_id, row[0], run_id, body.max_output_tokens))
        out = start_judge_run(conn, project_id, run_id, actor_id, model=body.model, budget_cap_micros=cap,
                              currency=body.currency, pricing_revision=body.pricing_revision, max_output_tokens=body.max_output_tokens)
    except NotFound:
        raise ApiError(404, "not_found", "run not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except JudgeRunError as exc:
        raise ApiError(409, "conflict", f"the judge run cannot start: {exc}") from None
    out["results_url"] = f"/api/v1/runs/{out['run_id']}/results"
    out["budget_derived"] = info is not None
    if info is not None:
        out["estimate"] = info
    return out


async def judge_handler(run_id: uuid.UUID, request: Request, conn: psycopg.Connection = Depends(get_db),
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
    return JSONResponse(status_code=202, content=await _run_blocking(request, "meta", _start_judge, conn, actor_id, project_id, run_id, body, request.app.state.settings))


router.add_api_route("/api/v1/runs/{run_id}/judge-runs", judge_handler, methods=["POST"], name="start_judge_run_v1", status_code=202)
