"""Job status (SPEC 8 ``GET /jobs/{id}``).

Read-only and operational: status, stage, attempts and the recent
transitions. Lease owner, lease token and expiry are never returned, since a
token would let a caller act as the worker. Transition
reasons are returned as recorded (a lease acquisition reason names the worker, never the token). The role matrix has no job
action yet, so access reuses ``read_gold_trace_and_lineage`` (owner,
maintainer, reviewer); a participant or observer who is a member gets 403 and
a non-member gets 404. PM is asked on Issue #6 whether that is the intent.
"""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
JOB_ACTION = "read_gold_trace_and_lineage"
MAX_TRANSITIONS = 50


def _get_job(conn: psycopg.Connection, actor_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    row = conn.execute(
        "SELECT project_id, kind, stage, status::text, row_version, attempt, max_attempts, retryable "
        "FROM jobs WHERE id = %s", (job_id,)).fetchone()
    missing = ApiError(404, "not_found", "job not found")
    if row is None:
        raise missing
    project_id, kind, stage, status, row_version, attempt, max_attempts, retryable = row
    try:
        authorize(conn, project_id, actor_id, JOB_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    rows = conn.execute(
        "SELECT from_status::text, to_status::text, reason, occurred_at FROM job_transitions "
        "WHERE job_id = %s ORDER BY id DESC LIMIT %s", (job_id, MAX_TRANSITIONS + 1)).fetchall()
    return {
        "id": str(job_id), "project_id": str(project_id), "kind": kind, "stage": stage, "status": status,
        "row_version": row_version, "attempt": attempt, "max_attempts": max_attempts, "retryable": retryable,
        "transitions": [{"from": a, "to": b, "reason": why, "occurred_at": at.isoformat()}
                        for a, b, why, at in rows[:MAX_TRANSITIONS]],  # newest first
        "transitions_truncated": len(rows) > MAX_TRANSITIONS,
    }


async def get_job_handler(job_id: uuid.UUID, request: Request,
                          conn: psycopg.Connection = Depends(get_db),
                          actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _get_job, conn, actor_id, job_id))


router.add_api_route("/api/v1/jobs/{job_id}", get_job_handler, methods=["GET"], name="get_job_v1")
