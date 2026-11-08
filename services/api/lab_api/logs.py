"""Log event query (SPEC 8 ``GET /logs``).

Filters: level, event name, trace, run, question, job, operation and a time
range on ``occurred_at``. Newest ingested first (event id) with a keyset
cursor bound to the project. Only events of the requested project are
returned (events without a project are never reachable here). ``attributes``
and ``error_message`` are not returned: they can carry participant-reported
text, and the redaction applied at write time is not a reason to expose them
through a list endpoint. ``message`` is free text; sensitive credentials
are redacted on retrieval according to the redaction policy while the append-only
database logs remain unchanged. There is no attempt filter because attempts are not
a column of ``log_events``.

Access reuses ``read_gold_trace_and_lineage`` like ``/jobs`` and
``/audit-events``; PM is asked on Issue #6.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .audit import bad, decode_cursor, encode_cursor, parse_limit, parse_time, parse_uuid
# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from lab_db.logs import redact  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
LOG_ACTION = "read_gold_trace_and_lineage"
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
EVENT_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
TRACE_RE = re.compile(r"[0-9a-f]{32}")
KNOWN_PARAMS = {"project_id", "level", "event_name", "trace_id", "run_id", "question_id", "job_id", "operation_id",
                "since", "until", "limit", "cursor"}


def parse_filters(params) -> dict:
    unknown = set(params) - KNOWN_PARAMS
    if unknown:
        raise bad(f"unknown query parameter: {sorted(unknown)[0][:40]}")
    f: dict = {}
    if "level" in params:
        if params["level"] not in LEVELS:
            raise bad(f"level must be one of {list(LEVELS)}")
        f["level"] = params["level"]
    if "event_name" in params:
        if len(params["event_name"]) > 128 or not EVENT_NAME_RE.fullmatch(params["event_name"]):
            raise bad("event_name is malformed")
        f["event_name"] = params["event_name"]
    if "trace_id" in params:
        if not TRACE_RE.fullmatch(params["trace_id"]):
            raise bad("trace_id must be 32 lowercase hex characters")
        f["trace_id"] = params["trace_id"]
    for name in ("run_id", "question_id", "job_id", "operation_id"):
        if name in params:
            f[name] = parse_uuid(name, params[name])
    for name in ("since", "until"):
        if name in params:
            f[name] = parse_time(name, params[name])
    if "since" in f and "until" in f and f["since"] >= f["until"]:
        raise bad("since must be before until")
    return f


COLUMNS = ("id", "event_id", "occurred_at", "level", "event_name", "message", "trace_id", "span_id", "parent_span_id",
           "service", "component", "operation_name", "operation_id", "job_id", "run_id", "question_id", "status",
           "duration_ms", "error_code", "telemetry_origin")
UUID_COLUMNS = {"event_id", "operation_id", "job_id", "run_id", "question_id"}


def _item(row: tuple) -> dict:
    item = {}
    for name, value in zip(COLUMNS, row):
        if name == "occurred_at":
            value = value.isoformat()
        elif name in UUID_COLUMNS and value is not None:
            value = str(value)
        elif name == "message" and isinstance(value, str):
            value = redact(value)
        item[name] = value
    return item


def _list_logs(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, filters: dict,
               cursor: Optional[str], limit: int) -> dict:
    try:
        authorize(conn, project_id, actor_id, LOG_ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "project not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    where, args = ["project_id = %s"], [project_id]
    for col, key, op in (("level", "level", "="), ("event_name", "event_name", "="), ("trace_id", "trace_id", "="),
                         ("run_id", "run_id", "="), ("question_id", "question_id", "="), ("job_id", "job_id", "="),
                         ("operation_id", "operation_id", "="), ("occurred_at", "since", ">="),
                         ("occurred_at", "until", "<")):
        if key in filters:
            where.append(f"{col} {op} %s")
            args.append(filters[key])
    if cursor is not None:
        where.append("id < %s")
        args.append(decode_cursor(cursor, project_id, endpoint="logs", filters=filters, ordering="id_desc"))
    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM log_events WHERE " + " AND ".join(where) +
                        " ORDER BY id DESC LIMIT %s", (*args, limit + 1)).fetchall()
    page = rows[:limit]
    return {"items": [_item(r) for r in page],
            "next_cursor": encode_cursor(project_id, page[-1][0], endpoint="logs", filters=filters, ordering="id_desc") if len(rows) > limit else None}


async def list_logs_handler(request: Request, conn: psycopg.Connection = Depends(get_db),
                            actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    params = request.query_params
    filters = parse_filters(params)
    limit = parse_limit(params.get("limit"))
    content = await _run_blocking(request, "meta", _list_logs, conn, actor_id, project_id, filters,
                                  params.get("cursor"), limit)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/logs", list_logs_handler, methods=["GET"], name="list_logs_v1")
