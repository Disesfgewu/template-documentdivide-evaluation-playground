"""Audit event query (SPEC 8 ``GET /audit-events``).

Filters: actor, resource, action, outcome and a time range; newest first with
a keyset cursor on the event id. The role matrix has no audit action, so
access reuses ``read_gold_trace_and_lineage`` (owner, maintainer, reviewer),
the same default as ``GET /jobs/{id}``; other members get 403 and non-members
404. Every query is scoped to one ``project_id`` and every value is bound as
a parameter. ``reason`` is free text written by actors; sensitive credentials
are redacted on retrieval according to the redaction policy while the append-only
database audit remains unchanged.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import json
import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from lab_db.logs import redact  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
AUDIT_ACTION = "read_gold_trace_and_lineage"
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
OUTCOMES = ("succeeded", "denied", "failed")
ACTION_RE = r"^[a-z][a-z0-9_.]{0,63}$"
KNOWN_PARAMS = {"project_id", "actor_id", "resource_id", "resource_type", "action", "outcome", "since", "until",
                "limit", "cursor"}


def bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def normalize_filters(filters: Optional[dict]) -> dict[str, str]:
    if not filters:
        return {}
    out: dict[str, str] = {}
    for k in sorted(filters):
        v = filters[k]
        if v is None:
            continue
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, datetime.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, str):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def encode_cursor(project_id: uuid.UUID, last_id: int, endpoint: str = "audit-events",
                  filters: Optional[dict] = None, ordering: str = "id_desc") -> str:
    raw = json.dumps({
        "e": endpoint,
        "f": normalize_filters(filters),
        "i": last_id,
        "o": ordering,
        "p": str(project_id),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, project_id: uuid.UUID, endpoint: str = "audit-events",
                  filters: Optional[dict] = None, ordering: str = "id_desc") -> int:
    err = ApiError(400, "invalid_cursor", "invalid cursor")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise err
    if len(cursor) % 4 == 1:
        raise err
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad)
    except (ValueError, binascii.Error):
        raise err from None
    try:
        canonical_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    except Exception:
        raise err from None
    if cursor != canonical_b64:
        raise err
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise err from None
    if (not isinstance(data, dict)
            or set(data) != {"e", "f", "i", "o", "p"}
            or data.get("p") != str(project_id)
            or data.get("e") != endpoint
            or data.get("o") != ordering
            or data.get("f") != normalize_filters(filters)
            or not isinstance(data.get("i"), int)
            or isinstance(data.get("i"), bool)
            or data["i"] < 1):
        raise err
    return data["i"]


def parse_limit(raw: Optional[str]) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 5 or not 1 <= int(raw) <= MAX_LIMIT:
        raise bad(f"limit must be an integer 1..{MAX_LIMIT}")
    return int(raw)


def parse_uuid(name: str, raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise bad(f"{name} must be a UUID") from None


def parse_time(name: str, raw: str) -> datetime.datetime:
    try:
        t = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise bad(f"{name} must be an ISO 8601 timestamp") from None
    if t.tzinfo is None:
        raise bad(f"{name} must include a UTC offset")
    return t


def parse_filters(params) -> dict:
    """Validated filters from the query string; unknown parameters are refused
    so a misspelt filter cannot silently widen the result."""
    unknown = set(params) - KNOWN_PARAMS
    if unknown:
        raise bad(f"unknown query parameter: {sorted(unknown)[0][:40]}")
    f: dict = {}
    for name in ("actor_id", "resource_id"):
        if name in params:
            f[name] = parse_uuid(name, params[name])
    if "resource_type" in params:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", params["resource_type"]):
            raise bad("resource_type is malformed")
        f["resource_type"] = params["resource_type"]
    if "action" in params:
        import re
        if not re.fullmatch(ACTION_RE, params["action"]):
            raise bad("action is malformed")
        f["action"] = params["action"]
    if "outcome" in params:
        if params["outcome"] not in OUTCOMES:
            raise bad(f"outcome must be one of {list(OUTCOMES)}")
        f["outcome"] = params["outcome"]
    for name in ("since", "until"):
        if name in params:
            f[name] = parse_time(name, params[name])
    if "since" in f and "until" in f and f["since"] >= f["until"]:
        raise bad("since must be before until")
    return f


def _list_events(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, filters: dict,
                 cursor: Optional[str], limit: int) -> dict:
    try:
        authorize(conn, project_id, actor_id, AUDIT_ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "project not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    where, args = ["project_id = %s"], [project_id]
    for col, key, op in (("actor_id", "actor_id", "="), ("resource_id", "resource_id", "="),
                         ("resource_type", "resource_type", "="), ("action", "action", "="),
                         ("outcome", "outcome", "="), ("occurred_at", "since", ">="), ("occurred_at", "until", "<")):
        if key in filters:
            where.append(f"{col} {op} %s")
            args.append(filters[key])
    if cursor is not None:
        where.append("id < %s")
        args.append(decode_cursor(cursor, project_id, endpoint="audit-events", filters=filters))
    rows = conn.execute(
        "SELECT id, event_id, actor_id, action, resource_type, resource_id, before_revision, after_revision, "
        "reason, outcome, operation_id, trace_id, occurred_at FROM audit_events WHERE " + " AND ".join(where) +
        " ORDER BY id DESC LIMIT %s", (*args, limit + 1)).fetchall()
    page = rows[:limit]
    return {
        "items": [{"id": r[0], "event_id": str(r[1]), "actor_id": str(r[2]), "action": r[3], "resource_type": r[4],
                   "resource_id": str(r[5]) if r[5] else None, "before_revision": r[6], "after_revision": r[7],
                   "reason": redact(r[8]) if isinstance(r[8], str) else r[8], "outcome": r[9],
                   "operation_id": str(r[10]), "trace_id": r[11],
                   "occurred_at": r[12].isoformat()} for r in page],
        "next_cursor": encode_cursor(project_id, page[-1][0], endpoint="audit-events", filters=filters) if len(rows) > limit else None,
    }


async def list_audit_events_handler(request: Request, conn: psycopg.Connection = Depends(get_db),
                                    actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    params = request.query_params
    filters = parse_filters(params)
    limit = parse_limit(params.get("limit"))
    content = await _run_blocking(request, "meta", _list_events, conn, actor_id, project_id, filters,
                                  params.get("cursor"), limit)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/audit-events", list_audit_events_handler, methods=["GET"], name="list_audit_events_v1")
