"""Job status (SPEC 8 ``GET /jobs/{id}``).

Read-only and operational: status, stage, attempts and the recent
transitions. Lease owner, lease token and expiry are never returned, since a
token would let a caller act as the worker. Transition reasons are bounded
in characters (UTF-8 max 4x bytes) at both the SQL and API projection layers.
Short reasons are redacted of credential-shaped text via ``lab_db.logs.redact``
while preserving benign context (original audit records in the database remain unchanged).
Overlong reasons fail closed to a fixed omission marker with explicit truncation
flag to prevent boundary secret leaks.
The role matrix has no job action yet, so access is scoped to staff roles
via ``read_gold_trace_and_lineage`` (owner, maintainer, reviewer), confirmed
by PM on Issue #6 as conservative scope pending participant run-binding;
a participant or observer who is a member gets 403 and a non-member gets 404.
"""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.jobs import JobNotCancellable, request_cancel  # noqa: E402
from lab_db.logs import redact  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
JOB_ACTION = "read_gold_trace_and_lineage"
MAX_TRANSITIONS = 50
MAX_REASON_CHARS = 2000
REASON_TRUNCATED_MARKER = "[omitted: reason exceeds maximum length]"


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
        "t.from_status::text, t.to_status::text, t.reason, t.occurred_at, t.reason_truncated "
        "FROM jobs j "
        "LEFT JOIN ("
        "    SELECT id, job_id, from_status, to_status, "
        "           CASE "
        "               WHEN char_length(reason) > %s THEN %s "
        "               ELSE reason "
        "           END AS reason, "
        "           occurred_at, "
        "           (char_length(reason) > %s) AS reason_truncated "
        "    FROM job_transitions "
        "    WHERE job_id = %s "
        "    ORDER BY id DESC "
        "    LIMIT %s"
        ") t ON t.job_id = j.id "
        "WHERE j.id = %s AND j.project_id = %s "
        "ORDER BY t.id DESC NULLS LAST",
        (MAX_REASON_CHARS, REASON_TRUNCATED_MARKER, MAX_REASON_CHARS, job_id, MAX_TRANSITIONS + 1, job_id, project_id),
    ).fetchall()
    if not rows:
        raise missing

    snap_project_id, kind, stage, status, row_version, attempt, max_attempts, retryable = rows[0][:8]
    transitions = []
    for r in rows:
        if r[8] is not None:
            raw_reason = r[10]
            db_truncated = bool(r[12]) if len(r) > 12 and r[12] is not None else False
            if db_truncated or (isinstance(raw_reason, str) and len(raw_reason) > MAX_REASON_CHARS):
                out_reason = REASON_TRUNCATED_MARKER
                is_truncated = True
            else:
                redacted = redact(raw_reason) if isinstance(raw_reason, str) else (raw_reason or "")
                if len(redacted) > MAX_REASON_CHARS:
                    out_reason = REASON_TRUNCATED_MARKER
                    is_truncated = True
                else:
                    out_reason = redacted
                    is_truncated = False

            transitions.append({
                "from": r[8],
                "to": r[9],
                "reason": out_reason,
                "reason_truncated": is_truncated,
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
    """Get job status and up to 50 recent transitions without lease tokens.

    Each reason is bounded to 2000 Unicode characters (at most 8000 UTF-8
    bytes). Overlong raw or redacted reasons are omitted entirely and carry
    ``reason_truncated=true``; short reasons are credential-redacted.
    Original database audit records are preserved. Staff project access only.
    """
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _get_job, conn, actor_id, job_id))


router.add_api_route("/api/v1/jobs/{job_id}", get_job_handler, methods=["GET"], name="get_job_v1",
                     summary="Get job status and recent transitions")


CANCEL_ACTION = "import_and_configure_parse"   # the same roles that may queue work (owner, maintainer)


def _cancel_job(conn: psycopg.Connection, actor_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    missing = ApiError(404, "not_found", "job not found")
    row = conn.execute("SELECT project_id FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise missing
    project_id = row[0]
    try:
        authorize(conn, project_id, actor_id, CANCEL_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    try:
        status, changed = request_cancel(conn, project_id, job_id, actor_id)
    except LookupError:
        raise missing from None
    except JobNotCancellable as exc:
        raise ApiError(409, "conflict", f"the job already finished ({exc.status}) and cannot be cancelled") from None
    return {"job_id": str(job_id), "status": status, "changed": changed}


async def cancel_job_handler(job_id: uuid.UUID, request: Request,
                             conn: psycopg.Connection = Depends(get_db),
                             actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    """Cancel a job: queued, paused or waiting jobs become ``cancelled`` at once; a running one becomes
    ``cancel_requested`` and its worker stops within about 2 seconds and acknowledges it. Repeating the
    request is harmless (``changed`` false); a finished job is a 409. Owner and maintainer only."""
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _cancel_job, conn, actor_id, job_id))


router.add_api_route("/api/v1/jobs/{job_id}/cancel", cancel_job_handler, methods=["POST"], name="cancel_job_v1",
                     summary="Cancel a job")
