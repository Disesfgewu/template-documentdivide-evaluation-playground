"""Review the generated questions and EXCLUDE some before the freeze (PM-DECISION on Issue #7, Refs #10/#12).

* ``GET  .../revisions/{revision}/questions/review``: owner/maintainer only (it shows validation verdicts, which are gold-protected).
  Every question, including the excluded ones, with its latest automated verdict and reason and its exclusion state.
* ``POST .../revisions/{revision}/questions/{question_id}/exclusion`` with ``{"excluded": true|false, "reason": "..."}``: owner only,
  only while the benchmark is ``validating``. A RECORDED decision, never a delete: question_exclusions is append-only (migration
  0019), the latest row per question wins, and an excluded question is not frozen, not answered and not scored. Restoring is
  another row. The freeze then needs every NON-excluded question to pass and at least one to remain."""
from __future__ import annotations

import uuid

import psycopg
import psycopg.errors
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from typing import Annotated

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import MAX_BODY_BYTES, _find
from lab_db.repo import NotFound, PermissionDenied, authorize

router = APIRouter()
BUILD_ACTION = "build_edit_freeze"


class ExclusionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    excluded: bool
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def _authorize(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, owner_only: bool) -> str:
    try:
        decision = authorize(conn, project_id, actor_id, BUILD_ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "benchmark revision not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if owner_only and decision.role != "owner":
        raise ApiError(403, "permission_denied", f"{decision.role} may not exclude a question: only the project owner may")
    return decision.role


def _review(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    _authorize(conn, project_id, actor_id, owner_only=False)
    benchmark_id, _, status, _ = _find(conn, project_id, logical_id, revision)
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))     # question_validations is gold-RLS
        rows = conn.execute(
            "SELECT q.id, q.ordinal, q.quota_bucket, q.question_text, v.verdict, v.reason, "
            "COALESCE(e.kind = 'exclude', false), e.reason FROM questions q "
            "LEFT JOIN LATERAL (SELECT verdict, reason FROM question_validations x WHERE x.question_id = q.id "
            "ORDER BY x.created_at DESC, x.id DESC LIMIT 1) v ON true "
            "LEFT JOIN LATERAL (SELECT kind, reason FROM question_exclusions x WHERE x.question_id = q.id ORDER BY x.seq DESC LIMIT 1) e ON true "
            "WHERE q.benchmark_id = %s ORDER BY q.ordinal", (benchmark_id,)).fetchall()
    items = [{"id": str(qid), "ordinal": int(ordinal), "quota_bucket": bucket, "question_text": text,
              "verdict": verdict or "unvalidated", "verdict_reason": vreason, "excluded": bool(excluded),
              "exclusion_reason": ereason if excluded else None}
             for qid, ordinal, bucket, text, verdict, vreason, excluded, ereason in rows]
    active = [i for i in items if not i["excluded"]]
    counts = {k: sum(1 for i in active if i["verdict"] == k) for k in ("pass", "fail", "needs_review", "unvalidated")}
    return {"benchmark_revision_id": str(benchmark_id), "status": status, "items": items,
            "total": len(items), "active": len(active), "excluded": len(items) - len(active),
            "validation_of_active": counts, "can_freeze": status == "validating" and bool(active) and counts["pass"] == len(active),
            "note": "verdicts are automated and come from the same provider and model as the generator; an exclusion is recorded with "
                    "its reason and never deletes the question"}


def _exclude(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
             question_id: uuid.UUID, body: ExclusionRequest) -> dict:
    _authorize(conn, project_id, actor_id, owner_only=True)
    event_id, op_id = uuid.uuid4(), uuid.uuid4()
    try:
        with conn.transaction():
            benchmark_id, _, status, _ = _find(conn, project_id, logical_id, revision, lock=True)
            if status != "validating":
                raise ApiError(409, "conflict", f"questions can only be excluded or restored while the benchmark is 'validating' (it is {status})")
            if conn.execute("SELECT 1 FROM questions WHERE benchmark_id = %s AND id = %s", (benchmark_id, question_id)).fetchone() is None:
                raise ApiError(404, "not_found", "question not found")
            last = conn.execute("SELECT kind FROM question_exclusions WHERE question_id = %s ORDER BY seq DESC LIMIT 1", (question_id,)).fetchone()
            now_excluded = last is not None and last[0] == "exclude"
            if now_excluded == body.excluded:
                raise ApiError(409, "conflict", "the question is already " + ("excluded" if now_excluded else "included"))
            kind = "exclude" if body.excluded else "restore"
            conn.execute("INSERT INTO question_exclusions (id, benchmark_id, question_id, kind, reason, actor_id) VALUES (%s, %s, %s, %s, %s, %s)",
                         (event_id, benchmark_id, question_id, kind, body.reason, actor_id))
            conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) VALUES (%s, %s, 'question.exclusion', %s, 'succeeded', now())",
                         (op_id, project_id, actor_id))
            conn.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, reason, outcome, operation_id) "
                         "VALUES (%s, %s, %s, %s, 'question', %s, %s, 'succeeded', %s)",
                         (uuid.uuid4(), project_id, actor_id, f"question.{kind}d", question_id, body.reason[:500], op_id))
            active = conn.execute("SELECT count(*) FROM active_questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege:
        raise ApiError(403, "permission_denied", "only the project owner may exclude a question") from None
    except psycopg.errors.IntegrityConstraintViolation:
        raise ApiError(409, "conflict", "the benchmark is no longer validating") from None
    return {"question_id": str(question_id), "excluded": body.excluded, "reason": body.reason, "active_questions": int(active),
            "note": "recorded, not deleted; an excluded question is not frozen, answered or scored"}


async def _body(request: Request) -> ExclusionRequest:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        return ExclusionRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", 'the body must be {"excluded": true|false, "reason": "<1..500 characters>"}') from None


async def review_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _review, conn, actor_id, project_id, logical_id, revision))


async def exclusion_handler(logical_id: str, revision: int, question_id: uuid.UUID, request: Request,
                            conn: psycopg.Connection = Depends(get_db),
                            actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    body = await _body(request)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _exclude, conn, actor_id, project_id, logical_id, revision, question_id, body))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions/review", review_handler, methods=["GET"],
                     name="review_questions_v1")
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions/{question_id}/exclusion", exclusion_handler,
                     methods=["POST"], name="exclude_question_v1")
