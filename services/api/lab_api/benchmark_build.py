"""Build a benchmark revision from a parsed document, up to ``planning`` (P-B, Refs #7, Refs #10). No LLM.

* ``POST /api/v1/benchmarks`` creates a revision for a document version (N and Q come from ``target_counts(page_count)``)
  and moves it ``draft -> parsing``.
* ``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/plan`` pins ONE succeeded parse job through
  ``promote_parse_result`` (G8 hard rule: only clean pages become evidence), moves ``parsing -> planning`` and returns
  the material plan; repeating it is idempotent.
* ``GET .../plan`` returns the same plan for roles that may read gold and generation context.

Access: creating and planning need ``build_edit_freeze`` with no extra condition (owner, maintainer); reading the plan
needs ``read_gold_and_generation_context``. A non-member and an unknown benchmark are both 404.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Optional

import psycopg
import psycopg.errors
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .material_plan import build_material_plan
from lab_contracts.benchmark import target_counts  # noqa: E402
from lab_db.promote import PromoteConflict, PromoteError, RESIDUAL_LIMIT, promote_parse_result  # noqa: E402
from lab_db.repo import Change, NotFound, PermissionDenied, authorize, record_change  # noqa: E402

router = APIRouter()
BENCHMARK_RESOURCE_TYPE = "benchmark_revision"
BUILD_ACTION = "build_edit_freeze"
READ_ACTION = "read_gold_and_generation_context"
MAX_BODY_BYTES = 16 * 1024
LOGICAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_version_id: str = Field(min_length=36, max_length=36)
    logical_id: Optional[str] = Field(default=None, pattern=LOGICAL_ID_PATTERN)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parse_job_id: Optional[str] = Field(default=None, min_length=36, max_length=36)


def _authorize(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, action: str, missing: ApiError) -> None:
    try:
        decision = authorize(conn, project_id, actor_id, action)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if action == BUILD_ACTION and decision.condition is not None:    # e.g. a reviewer may only comment
        raise ApiError(403, "permission_denied", f"{decision.role} may not build a benchmark")


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(400, "bad_request", f"invalid {what}") from None


def _transition(conn: psycopg.Connection, actor_id: uuid.UUID, benchmark_id: uuid.UUID, to_status: str, reason: str) -> None:
    conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
    conn.execute("SELECT set_config('lab.transition_reason', %s, true)", (reason,))
    conn.execute("UPDATE benchmark_revisions SET status = %s, row_version = row_version + 1 WHERE id = %s",
                 (to_status, benchmark_id))


def _create(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, body: CreateRequest) -> dict:
    missing = ApiError(404, "not_found", "document not found")
    document_version_id = _uuid(body.document_version_id, "document_version_id")
    _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
    row = conn.execute("SELECT resource_id, page_count FROM document_versions WHERE project_id = %s AND id = %s",
                       (project_id, document_version_id)).fetchone()
    if row is None:
        raise missing
    document_resource_id, page_count = row
    n_target, q_min = target_counts(page_count)
    logical_id = body.logical_id or "bench-" + uuid.uuid4().hex[:10]
    content_hash = hashlib.sha256(json.dumps(
        {"document_version_id": str(document_version_id), "n_target": n_target, "q_min": q_min},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    benchmark_id = uuid.uuid4()
    try:
        with conn.transaction():
            _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)      # again at the moment of writing
            resource_id = record_change(conn, Change(
                project_id, actor_id, "benchmark.create", BENCHMARK_RESOURCE_TYPE, logical_id, 1, content_hash,
                "lab-c14n-1", "benchmark.created", lineage=[(document_resource_id, "derived_from")]))
            conn.execute(
                "INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) "
                "VALUES (%s, %s, %s, %s, %s, %s)", (benchmark_id, project_id, resource_id, document_version_id, n_target, q_min))
            _transition(conn, actor_id, benchmark_id, "parsing", "benchmark created: waiting for its parse to be pinned")
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "conflict", "a benchmark with this logical_id already exists") from None
    return {"benchmark_revision_id": str(benchmark_id), "logical_id": logical_id, "revision": 1, "status": "parsing",
            "document_version_id": str(document_version_id), "n_target": n_target, "q_min": q_min}


def _find(conn: psycopg.Connection, project_id: uuid.UUID, logical_id: str, revision: int, lock: bool = False) -> tuple:
    row = conn.execute(
        "SELECT br.id, br.document_version_id, br.status::text, dv.page_count FROM benchmark_revisions br "
        "JOIN resource_versions rv ON rv.id = br.resource_id JOIN document_versions dv ON dv.id = br.document_version_id "
        "WHERE br.project_id = %s AND rv.resource_type = %s AND rv.logical_id = %s AND rv.revision = %s"
        + (" FOR UPDATE OF br" if lock else ""), (project_id, BENCHMARK_RESOURCE_TYPE, logical_id, revision)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "benchmark revision not found")
    return row


def _report(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, logical_id: str, revision: int,
            status: str, page_count: int) -> dict:
    pin = conn.execute("SELECT parse_job_id, rule, provenance FROM benchmark_parse_sources WHERE benchmark_id = %s",
                       (benchmark_id,)).fetchone()
    if pin is None:
        raise ApiError(409, "conflict", "the benchmark has no pinned parse yet; POST .../plan first")
    job_id, rule, prov = pin
    units: dict[int, dict[str, int]] = {}
    for page, kind, count in conn.execute(
            "SELECT s.page_index, er.kind, count(DISTINCT eu.id) FROM evidence_unit_sources s "
            "JOIN evidence_units eu ON eu.id = s.evidence_unit_id "
            "JOIN evidence_regions er ON er.evidence_unit_id = eu.id AND er.ordinal = 0 "
            "WHERE s.project_id = %s AND s.parse_job_id = %s GROUP BY 1, 2", (project_id, job_id)):
        units.setdefault(page, {})[kind] = int(count)
    plan = build_material_plan(page_count, [int(p) for p in prov["promoted_pages"]],
                               {int(k): v for k, v in prov["skipped_pages"].items()}, units, prov["residual_limit"])
    plan.update({"benchmark_revision_id": str(benchmark_id), "logical_id": logical_id, "revision": revision, "status": status,
                 "parse_job_id": str(job_id), "promotion_rule": rule})
    return plan


def _plan(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
          body: PlanRequest) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
    asked = _uuid(body.parse_job_id, "parse_job_id") if body.parse_job_id else None
    with conn.transaction():
        _authorize(conn, project_id, actor_id, BUILD_ACTION, missing)
        benchmark_id, document_version_id, status, page_count = _find(conn, project_id, logical_id, revision, lock=True)
        pinned = conn.execute("SELECT parse_job_id FROM benchmark_parse_sources WHERE benchmark_id = %s",
                              (benchmark_id,)).fetchone()
        if pinned is not None:
            if asked is not None and asked != pinned[0]:
                raise ApiError(409, "conflict", "the benchmark already pins another parse job")
            return _report(conn, project_id, benchmark_id, logical_id, revision, status, page_count)
        if status != "parsing":
            raise ApiError(409, "conflict", f"a benchmark in status {status} takes no parse pin")
        if asked is None:
            row = conn.execute(
                "SELECT j.id FROM jobs j JOIN parse_results pr ON pr.project_id = j.project_id AND pr.job_id = j.id "
                "WHERE j.project_id = %s AND pr.document_version_id = %s AND j.status::text = 'succeeded' "
                "ORDER BY pr.created_at DESC LIMIT 1", (project_id, document_version_id)).fetchone()
            if row is None:
                raise ApiError(409, "conflict", "no succeeded parse for this document version; POST /api/v1/documents/{id}/parse first")
            asked = row[0]
        try:
            promote_parse_result(conn, project_id, benchmark_id, asked, actor_id)
        except PromoteConflict:
            raise ApiError(409, "conflict", "the benchmark already pins another parse job") from None
        except PromoteError as exc:
            raise ApiError(409, "conflict", f"the parse job cannot be pinned: {exc}") from None
        _transition(conn, actor_id, benchmark_id, "planning", "parse pinned and promoted; material plan built")
        return _report(conn, project_id, benchmark_id, logical_id, revision, "planning", page_count)


def _get_plan(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    _authorize(conn, project_id, actor_id, READ_ACTION, missing)
    benchmark_id, _, status, page_count = _find(conn, project_id, logical_id, revision)
    return _report(conn, project_id, benchmark_id, logical_id, revision, status, page_count)


async def _json_body(request: Request, model):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        return model() if raw.strip() == b"" else model.model_validate_json(raw)
    except (ValidationError, TypeError):
        raise ApiError(400, "bad_request", "invalid request body") from None


async def create_handler(request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        body = CreateRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", "invalid request body") from None
    return JSONResponse(status_code=201, content=await _run_blocking(request, "meta", _create, conn, actor_id, project_id, body))


async def plan_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                       actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    body = await _json_body(request, PlanRequest)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _plan, conn, actor_id, project_id, logical_id, revision, body))


async def get_plan_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _get_plan, conn, actor_id, project_id, logical_id, revision))


router.add_api_route("/api/v1/benchmarks", create_handler, methods=["POST"], name="create_benchmark_v1", status_code=201)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/plan", plan_handler, methods=["POST"], name="plan_benchmark_v1")
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/plan", get_plan_handler, methods=["GET"], name="get_benchmark_plan_v1")
