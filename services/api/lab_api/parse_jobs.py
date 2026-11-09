"""Request a parse of a stored document (``POST /api/v1/documents/{id}/parse``).

Creates a queued ``document.parse`` job bound to its input (document version,
original page indices, profile revision, optional budget reference) in one PG
transaction with its operation, idempotency key and audit event. Nothing is
parsed here and no provider is called: a worker leases the job later. Access
follows the role matrix action ``import_and_configure_parse`` (owner and
maintainer); a non-member and an unknown document are both 404, a member
without the role gets 403. At most one parse job per document version may be queued or running (409 ``conflict`` with
the active ``job_id`` otherwise). The same ``Idempotency-Key`` with the same request
replays the stored original response (200, ``replayed``); with a different
request it is a 409.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import (
    ApiError,
    _replay_body,
    _run_blocking,
    _save_result,
    get_authenticated_actor,
    get_db,
)
from lab_db.jobs import ParseJobActive, enqueue_parse_job  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
PARSE_ACTION = "import_and_configure_parse"
MAX_BODY_BYTES = 64 * 1024
DEFAULT_PROFILE = "unstructured-oss-fast"
PROFILE_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"


class ParseRequest(BaseModel):
    """Strict body: unknown fields, wrong types and booleans-as-integers are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    profile: str = Field(default=DEFAULT_PROFILE, pattern=PROFILE_PATTERN)
    page_selection: Optional[list[int]] = Field(default=None, min_length=1, max_length=2000)
    budget_ref: Optional[str] = Field(default=None, min_length=1, max_length=128)


def _authorize_parse(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, missing: ApiError) -> None:
    try:
        authorize(conn, project_id, actor_id, PARSE_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None


def _validate_selection(selection: Optional[list[int]], page_count: int) -> None:
    if selection is None:
        return
    if any(type(p) is not int or not 0 <= p < page_count for p in selection):
        raise ApiError(400, "bad_request", f"page_selection indices must be integers in [0, {page_count - 1}]")
    if any(b <= a for a, b in zip(selection, selection[1:])):
        raise ApiError(400, "bad_request", "page_selection must be strictly ascending original page indices")


def _request_parse(
    conn: psycopg.Connection,
    actor_id: uuid.UUID,
    document_id: uuid.UUID,
    body: ParseRequest,
    idempotency_key: Optional[str],
    request_id: str,
) -> tuple[int, dict]:
    missing = ApiError(404, "not_found", "document not found")
    row = conn.execute("SELECT project_id, page_count FROM document_versions WHERE id = %s", (document_id,)).fetchone()
    if row is None:
        raise missing
    project_id, page_count = row
    _authorize_parse(conn, project_id, actor_id, missing)
    _validate_selection(body.page_selection, page_count)

    canonical = json.dumps(
        {"document": str(document_id), "profile": body.profile, "pages": body.page_selection, "budget": body.budget_ref},
        sort_keys=True, separators=(",", ":"),
    )
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
        # Authorize again at the moment of writing (membership may have changed).
        _authorize_parse(conn, project_id, actor_id, missing)
        if idempotency_key:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (f"idempotency:{project_id}:parse:{idempotency_key}",))
            existing = conn.execute(
                "SELECT ik.request_hash, ik.operation_id, op.status FROM idempotency_keys ik "
                "JOIN operations op ON op.project_id = ik.project_id AND op.id = ik.operation_id "
                "WHERE ik.project_id = %s AND ik.idempotency_scope = 'document.parse' AND ik.idempotency_key = %s",
                (project_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_hash, existing_op, op_status = existing
                if existing_hash != request_hash:
                    raise ApiError(409, "conflict", "idempotency key already used with a different request")
                if op_status != "succeeded":
                    raise ApiError(409, "conflict", "idempotency key operation currently in progress")
                stored = conn.execute(
                    "SELECT http_status, response_body FROM operation_results WHERE project_id = %s AND operation_id = %s",
                    (project_id, existing_op),
                ).fetchone()
                if stored is None:
                    raise ApiError(500, "internal_error", "stored parse response not found")
                return _replay_body(stored[0], stored[1], request_id)

        operation_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'document.parse', %s, 'intent')",
            (operation_id, project_id, actor_id),
        )
        if idempotency_key:
            conn.execute(
                "INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id) "
                "VALUES (%s, 'document.parse', %s, %s, %s)",
                (project_id, idempotency_key, request_hash, operation_id),
            )
        try:
            job_id = enqueue_parse_job(
                conn, project_id=project_id, actor_id=actor_id, operation_id=operation_id,
                document_version_id=document_id, profile=body.profile,
                page_selection=body.page_selection, budget_ref=body.budget_ref,
            )
        except ParseJobActive as active:
            # Rolls the whole transaction back: no operation or idempotency key is stored,
            # so the client can retry the same key once the active job has finished.
            raise ApiError(409, "conflict", "a parse job for this document version is already active",
                           details={"job_id": str(active.job_id)}) from None
        created = {
            "job_id": str(job_id),
            "document_version_id": str(document_id),
            "project_id": str(project_id),
            "kind": "document.parse",
            "status": "queued",
            "profile": body.profile,
            "page_selection": body.page_selection,
            "budget_ref": body.budget_ref,
        }
        _save_result(conn, project_id, operation_id, 202, created)
        return 202, created


async def parse_document_handler(
    document_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    """Queue a parse job for a stored document version; the worker runs it later."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    if raw.strip() == b"":
        body = ParseRequest()
    else:
        try:
            body = ParseRequest.model_validate_json(raw)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()})
            raise ApiError(400, "bad_request", "invalid parse request", details={"fields": fields}) from None
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 1 <= len(key) <= 256:
        raise ApiError(400, "bad_request", "Idempotency-Key length must be between 1 and 256 characters")
    request_id = request.headers.get("X-Request-Id", "req-1")
    status, content = await _run_blocking(request, "meta", _request_parse, conn, actor_id, document_id, body, key, request_id)
    return JSONResponse(status_code=status, content=content)


router.add_api_route(
    "/api/v1/documents/{document_id}/parse", parse_document_handler, methods=["POST"],
    name="parse_document_v1", summary="Queue a parse job for a document version", status_code=202,
)
