"""Start a run of the answering method on a frozen benchmark (PM-DECISION on Issue #7, item (a), Refs #12).

``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs`` (owner/maintainer; the benchmark must be frozen; body as
for generation: model, budget_cap_micros [this run's budget], max_output_tokens <= 500): creates the method revision, the run and
an ``answer`` job and returns 202. The API makes no model call: the generation worker runs the job. Read the run with
``GET /api/v1/runs/{id}`` and its scores with ``GET /api/v1/runs/{id}/results`` (``submissions`` counts answered / model
abstentions / method failures separately). A second concurrent method run on the same benchmark is refused (409)."""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import MAX_BODY_BYTES, _find
from .benchmark_generate import GenerateRequest
from .answer_run import AnswerRunError, start_answer_run
from lab_db.repo import NotFound, PermissionDenied

router = APIRouter()


def _start(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
           body: GenerateRequest) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    try:
        benchmark_id, _, _, _ = _find(conn, project_id, logical_id, revision)
        out = start_answer_run(conn, project_id, benchmark_id, actor_id, model=body.model, budget_cap_micros=body.budget_cap_micros,
                               currency=body.currency, pricing_revision=body.pricing_revision,
                               max_output_tokens=body.max_output_tokens)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except AnswerRunError as exc:
        raise ApiError(409, "conflict", f"the method run cannot start: {exc}") from None
    out["results_url"] = f"/api/v1/runs/{out['run_id']}/results"
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
        request, "meta", _start, conn, actor_id, project_id, logical_id, revision, body))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs", start_handler, methods=["POST"],
                     name="start_method_run_v1", status_code=202)
