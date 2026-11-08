"""Job status (SPEC 8 ``GET /jobs/{id}``).

Read-only and operational: status, stage, attempts and the recent
transitions. Lease owner, lease token and expiry are never returned, since a
token would let a caller act as the worker. Transition reasons are redacted
of credential-shaped text via ``lab_db.logs.redact`` while preserving benign context
(original audit records in the database remain unchanged).
The role matrix has no job action yet, so access reuses
``read_gold_trace_and_lineage`` (owner, maintainer, reviewer); a participant
or observer who is a member gets 403 and a non-member gets 404. PM is asked
on Issue #6 whether that is the intent.
"""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.logs import redact  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
JOB_ACTION = "read_gold_trace_and_lineage"
MAX_TRANSITIONS = 50


def _get_job(conn: psycopg.Connection, actor_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    missing = ApiError(404, "not_found", "job not found")
    row = conn.execute("SELECT project_id FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise missing
    project_id = row[0]
    try:
        authorize(conn, project_id, actor_id, JOB_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None

    rows = conn.execute(
        "SELECT j.project_id, j.kind, j.stage, j.status::text, j.row_version, j.attempt, j.max_attempts, j.retryable, "
        "t.from_status::text, t.to_status::text, t.reason, t.occurred_at "
        "FROM jobs j "
        "LEFT JOIN ("
        "    SELECT id, job_id, from_status, to_status, reason, occurred_at "
        "    FROM job_transitions "
        "    WHERE job_id = %s "
        "    ORDER BY id DESC "
        "    LIMIT %s"
        ") t ON t.job_id = j.id "
        "WHERE j.id = %s AND j.project_id = %s "
        "ORDER BY t.id DESC NULLS LAST",
        (job_id, MAX_TRANSITIONS + 1, job_id, project_id),
    ).fetchall()
    if not rows:
        raise missing

    snap_project_id, kind, stage, status, row_version, attempt, max_attempts, retryable = rows[0][:8]
    transitions = []
    for r in rows:
        if r[8] is not None:
            transitions.append({
                "from": r[8],
                "to": r[9],
                "reason": redact(r[10]) if isinstance(r[10], str) else (r[10] or ""),
                "occurred_at": r[11].isoformat() if hasattr(r[11], "isoformat") else str(r[11]),
            })

    return {
        "id": str(job_id),
        "project_id": str(snap_project_id),
        "kind": kind,
        "stage": stage,
        "status": status,
        "row_version": row_version,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retryable": retryable,
        "transitions": transitions[:MAX_TRANSITIONS],  # newest first
        "transitions_truncated": len(transitions) > MAX_TRANSITIONS,
    }


async def get_job_handler(job_id: uuid.UUID, request: Request,
                          conn: psycopg.Connection = Depends(get_db),
                          actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _get_job, conn, actor_id, job_id))


router.add_api_route("/api/v1/jobs/{job_id}", get_job_handler, methods=["GET"], name="get_job_v1")
