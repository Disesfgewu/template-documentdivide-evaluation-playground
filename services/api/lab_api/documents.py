"""Document ingestion, versioning, and download endpoints (SPEC R01, 8, 10.1-10.2, 16.4)."""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _sub in (_REPO_ROOT / "db", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import anyio
import anyio.to_thread
import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from lab_db.ingest import IngestRejected, Inspection, PageGeometry, ingest_document
from lab_db.repo import NotFound, PermissionDenied, authorize
from lab_db.storage import LocalArtifactStore, StorageCorruption
from lab_parser.artifacts import inspect_pdf_isolated
from lab_parser.importer import Limits

from .auth import AuthError, get_signing_secret, verify_actor_token
from .config import Settings
from .multipart_strict import MultipartError, parse_upload as parse_multipart
from .pdf_body import (
    DEFAULT_MAX_PDF_BYTES,
    PdfRequestBodyError,
    read_pdf_request_body,
)
from .preview import DEFAULT_PREVIEW_TIMEOUT_S, PreviewError, render_preview_in_subprocess
from .readiness import connect

DEFAULT_MAX_PDF_PAGES = 2000


class ApiError(Exception):
    """Sanitized API error following the SPEC 16.4 ErrorResponse contract."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable

    def to_error_response(self, request_id: str = "req-1") -> JSONResponse:
        content = {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": request_id,
                "details": self.details,
                "retry_after": None,
            }
        }
        return JSONResponse(status_code=self.status_code, content=content)


REJECTION_CODE_MAP = {
    "empty_file": ("empty_file", 422),
    "not_a_pdf": ("invalid_pdf", 422),
    "corrupt_pdf": ("corrupt_pdf", 422),
    "encrypted_pdf": ("unsupported_encryption", 422),
    "page_limit_exceeded": ("page_limit_exceeded", 422),
    "file_too_large": ("payload_too_large", 413),
}


def _rejection_error(
    reason: str,
    *,
    sha256: str = "",
    size_bytes: int = 0,
    page_count: Optional[int] = None,
    error_type: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """The rejection's HTTP status and error object, without the per-request
    ``request_id``. This exact object is stored with the operation and
    replayed to idempotent retries."""
    if reason == "invalid_page_geometry":
        return 422, {
            "code": "invalid_page_geometry",
            "message": "failed to parse page geometry from PDF",
            "retryable": False,
            "details": {"error": error_type or "ValueError"},
            "retry_after": None,
        }

    code, status_code = REJECTION_CODE_MAP.get(reason, ("invalid_pdf", 422))
    return status_code, {
        "code": code,
        "message": f"document rejected: {reason}",
        "retryable": False,
        "details": {
            "reason": reason,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "page_count": page_count,
        },
        "retry_after": None,
    }


def _make_rejection_error_response(
    reason: str,
    *,
    request_id: str = "req-1",
    sha256: str = "",
    size_bytes: int = 0,
    page_count: Optional[int] = None,
    error_type: Optional[str] = None,
) -> JSONResponse:
    status_code, error = _rejection_error(
        reason, sha256=sha256, size_bytes=size_bytes, page_count=page_count, error_type=error_type
    )
    return JSONResponse(status_code=status_code, content={"error": {**error, "request_id": request_id}})


def get_db(request: Request):
    """Yield a database connection from application settings."""
    settings: Settings = request.app.state.settings
    try:
        with connect(settings) as conn:
            yield conn
    except psycopg.Error as exc:
        raise ApiError(
            status_code=503,
            code="database_unavailable",
            message=f"database connection failed ({type(exc).__name__})",
        ) from exc


def get_store(request: Request) -> LocalArtifactStore:
    """Instantiate the LocalArtifactStore from settings."""
    settings: Settings = request.app.state.settings
    try:
        return LocalArtifactStore(settings.artifact_root)
    except Exception as exc:
        raise ApiError(
            status_code=503,
            code="storage_unavailable",
            message=f"artifact storage unavailable ({type(exc).__name__})",
        ) from exc


def get_authenticated_actor(
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
) -> uuid.UUID:
    """Authenticate the caller via cryptographically signed Bearer token.

    Rejects caller-asserted identity (e.g. raw UUIDs, X-Actor-Id spoofing headers)
    and verifies that the extracted actor exists in PostgreSQL.
    """
    if "X-Actor-Id" in request.headers:
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="caller-asserted identity headers are not permitted",
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="missing Authorization header",
        )

    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="invalid Authorization header format, expected Bearer <token>",
        )

    raw_token = parts[1]
    settings: Settings = request.app.state.settings
    try:
        secret = get_signing_secret(settings)
    except AuthError as exc:
        raise ApiError(status_code=exc.status_code, code=exc.code, message=exc.message)

    actor_id = verify_actor_token(raw_token, secret)
    if actor_id is None:
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="invalid or untrusted actor credentials",
        )

    row = conn.execute(
        "SELECT 1 FROM actors WHERE id = %s",
        (actor_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="unknown or invalid actor",
        )

    return actor_id


def resolve_project_id(
    request: Request,
    path_project_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """Resolve project_id from path parameter, query parameter, or X-Project-Id header."""
    if path_project_id is not None:
        return path_project_id

    qp = request.query_params.get("project_id")
    if qp:
        try:
            return uuid.UUID(qp)
        except ValueError:
            raise ApiError(400, "bad_request", "invalid project_id format")

    hp = request.headers.get("X-Project-Id")
    if hp:
        try:
            return uuid.UUID(hp)
        except ValueError:
            raise ApiError(400, "bad_request", "invalid project_id format")

    raise ApiError(400, "bad_request", "missing required project_id")


def resolve_logical_id(request: Request, form_logical_id: Optional[str] = None) -> str:
    """Resolve logical_id from form, query parameter or X-Logical-Id header; defaults to 'default'."""
    val = form_logical_id or request.query_params.get("logical_id") or request.headers.get("X-Logical-Id")
    if not val:
        return "default"
    if not (1 <= len(val) <= 128):
        raise ApiError(
            400, "bad_request", "logical_id length must be between 1 and 128 characters"
        )
    return val


router = APIRouter()

# ---------------------------------------------------------------------------
# Blocking work, admission and memory bounds (SPEC 12.3, 16.6; PM review on #73)
#
# Everything that blocks (multipart parsing, sha256, PDF inspection, SQL,
# artifact storage) runs in a worker thread through ``_run_blocking``: if the
# request is cancelled (disconnect, shutdown, timeout) the cancellation waits
# until the thread has finished. The request-scoped connection and store
# therefore cannot be closed underneath a running worker
# (``loop.run_in_executor`` returns on cancel while the thread keeps going,
# which is the defect found in the first attempt).
#
# Memory is bounded by admission, not by thread counts: a request must take a
# slot *before* its body is read, and slots are refused (503, retryable)
# when none is free. Worst case is about
#   upload_concurrency * 2 * (max_pdf_bytes + MULTIPART_OVERHEAD_BYTES)
# because a multipart body is held once and its PDF part is copied once.
# ---------------------------------------------------------------------------
DEFAULT_UPLOAD_CONCURRENCY = 2
DEFAULT_DOWNLOAD_CONCURRENCY = 2
DEFAULT_PREVIEW_CONCURRENCY = 2
DEFAULT_METADATA_CONCURRENCY = 8
MULTIPART_OVERHEAD_BYTES = 65536
# An admitted upload must deliver its body within this many seconds, or it
# loses its slot (408). Without it two slow clients could hold every slot.
DEFAULT_UPLOAD_READ_TIMEOUT_S = 600.0


def _state_object(request: Request, name: str, factory):
    """Lazily create one shared primitive on app.state (single event loop
    thread, no await between check and set, so this is race-free)."""
    obj = getattr(request.app.state, name, None)
    if obj is None:
        obj = factory()
        setattr(request.app.state, name, obj)
    return obj


def _positive_int(request: Request, name: str, default: int) -> int:
    value = getattr(request.app.state, name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else default


async def _run_blocking(request: Request, pool: str, fn, *args, cancel: "threading.Event | None" = None):
    """Run ``fn(*args)`` in a worker thread; cancellation is deferred until it ends.

    ``cancel`` (optional) is set the moment the request is cancelled (client disconnect,
    shutdown, timeout) so a long worker, for example the PDF isolation child, can stop early
    instead of running to its own deadline while the request waits for it.
    """
    size = DEFAULT_METADATA_CONCURRENCY
    if pool == "upload":
        size = _positive_int(request, "upload_concurrency", DEFAULT_UPLOAD_CONCURRENCY)
    elif pool == "download":
        size = _positive_int(request, "download_concurrency", DEFAULT_DOWNLOAD_CONCURRENCY)
    elif pool == "preview":
        size = _positive_int(request, "preview_concurrency", DEFAULT_PREVIEW_CONCURRENCY)
    limiter = _state_object(request, f"{pool}_threads", lambda: anyio.CapacityLimiter(size))
    # The thread runs as its own task so that cancelling *this* task, by
    # anyio's cancel scopes or by a plain task.cancel(), cannot abandon it:
    # we wait for the worker (which owns the request's connection) before
    # letting the cancellation continue.
    job = asyncio.ensure_future(
        anyio.to_thread.run_sync(functools.partial(fn, *args), abandon_on_cancel=False, limiter=limiter)
    )
    try:
        await asyncio.wait({job})  # cancelling this wait never cancels the job
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.set()
        with anyio.CancelScope(shield=True):
            while not job.done():
                try:
                    await asyncio.wait({job})
                except asyncio.CancelledError:
                    continue
        if not job.cancelled():
            job.exception()  # mark retrieved; the request is gone, nobody can use it
        raise
    return job.result()


def _take_slot(request: Request, pool: str, default: int, what: str) -> anyio.Semaphore:
    """Take an admission slot without waiting; release it in ``finally``."""
    size = _positive_int(request, f"{pool}_concurrency", default)
    sem = _state_object(request, f"{pool}_slots", lambda: anyio.Semaphore(size))
    try:
        sem.acquire_nowait()
    except anyio.WouldBlock:
        raise ApiError(
            503, "server_busy", f"too many concurrent {what}; retry later", retryable=True
        ) from None
    return sem


@dataclasses.dataclass(frozen=True)
class RawUpload:
    """A bounded request body that has been read but not yet parsed."""

    kind: str  # "pdf" or "multipart"
    content_type: str
    body: "bytes | bytearray"


async def read_upload_body(request: Request, max_bytes: int) -> RawUpload:
    """Read the request body under a hard byte bound (no parsing, no copying)."""
    raw_ct = request.headers.get("Content-Type", "").strip()
    ct_lower = raw_ct.lower()

    if ct_lower.startswith("application/pdf"):
        try:
            body = await read_pdf_request_body(request, max_bytes=max_bytes, as_bytearray=True)
        except PdfRequestBodyError as exc:
            # One code for "body too large" on every upload path.
            code = "payload_too_large" if exc.code == "file_too_large" else exc.code
            raise ApiError(status_code=exc.status_code, code=code, message=exc.message) from exc
        return RawUpload("pdf", raw_ct, body)

    if ct_lower.startswith("multipart/form-data"):
        try:
            body = await read_pdf_request_body(
                request,
                max_bytes=max_bytes + MULTIPART_OVERHEAD_BYTES,
                media_type=b"multipart/form-data",
                as_bytearray=True,
            )
        except PdfRequestBodyError as exc:
            code = "payload_too_large" if exc.code == "file_too_large" else exc.code
            raise ApiError(status_code=exc.status_code, code=code, message=exc.message) from exc
        return RawUpload("multipart", raw_ct, body)

    raise ApiError(415, "unsupported_media_type", "Content-Type must be application/pdf or multipart/form-data")


async def _read_upload_within_deadline(request: Request, max_bytes: int) -> RawUpload:
    timeout = getattr(request.app.state, "upload_read_timeout_s", DEFAULT_UPLOAD_READ_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_UPLOAD_READ_TIMEOUT_S
    try:
        with anyio.fail_after(timeout):
            return await read_upload_body(request, max_bytes)
    except TimeoutError:
        raise ApiError(
            408, "request_timeout", "the request body was not received in time", retryable=True
        ) from None


def parse_upload(raw: RawUpload, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Blocking: return (PDF bytes, optional form logical_id) from a bounded body."""
    if raw.kind == "pdf":
        return bytes(raw.body), None
    try:
        parsed = parse_multipart(raw.content_type, raw.body, max_bytes)
    except MultipartError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    return parsed.pdf, parsed.logical_id


async def extract_upload_data(request: Request, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Read and parse an upload (kept for callers and tests that want both steps)."""
    raw = await read_upload_body(request, max_bytes)
    return await _run_blocking(request, "upload", parse_upload, raw, max_bytes)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _UploadContext:
    project_id: uuid.UUID
    actor_id: uuid.UUID
    max_bytes: int
    max_pages: int
    idempotency_key: Optional[str]
    request_id: str
    logical_hint: Optional[str]  # query parameter or X-Logical-Id header
    cancel: Optional[threading.Event] = None  # set when the client goes away


def _authorize_upload(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    try:
        authorize(conn, project_id, actor_id, "import_and_configure_parse")
    except NotFound:
        raise ApiError(404, "not_found", "project not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))


def _pick_logical_id(form_logical_id: Optional[str], hint: Optional[str]) -> str:
    val = form_logical_id or hint
    if not val:
        return "default"
    if not (1 <= len(val) <= 128):
        raise ApiError(400, "bad_request", "logical_id length must be between 1 and 128 characters")
    return val


def _save_result(conn, project_id, operation_id, status: int, body: dict) -> None:
    """Persist the original response with its operation (same transaction as
    the outcome) so a retry replays it verbatim. A rejection stores its error
    object (no request_id); a success stores its body."""
    conn.execute(
        "INSERT INTO operation_results (project_id, operation_id, http_status, response_body) "
        "VALUES (%s, %s, %s, %s)",
        (project_id, operation_id, status, Jsonb(body)),
    )


def _replay_body(status: int, stored: dict, request_id: str) -> tuple[int, dict]:
    """The response a retry gets from a stored original."""
    if status >= 400:
        return status, {"error": {**stored, "request_id": request_id}}
    return 200, {**stored, "replayed": True}


_UNSET = object()


def _upload_core(
    conn: psycopg.Connection,
    store: LocalArtifactStore,
    ctx: _UploadContext,
    raw: RawUpload,
) -> tuple[int, dict]:
    """Blocking upload body: parse, hash, inspect, persist. Returns (status, JSON)."""
    data, form_logical_id = parse_upload(raw, ctx.max_bytes)
    logical_id = _pick_logical_id(form_logical_id, ctx.logical_hint)
    proj_id, actor_id = ctx.project_id, ctx.actor_id
    pdf_sha256 = hashlib.sha256(data).hexdigest()
    request_hash = hashlib.sha256(f"{logical_id}:{pdf_sha256}".encode("utf-8")).hexdigest()
    idempotency_key = ctx.idempotency_key
    op_intent_id: Optional[uuid.UUID] = None

    def reject(reason, *, sha256="", size_bytes=0, page_count=None, error_type=None, warnings=(), db_reason=_UNSET):
        status, error = _rejection_error(
            reason, sha256=sha256, size_bytes=size_bytes, page_count=page_count, error_type=error_type
        )
        db_inspection = Inspection(
            outcome="reject", reason=reason if db_reason is _UNSET else db_reason, sha256=sha256,
            size_bytes=size_bytes, page_count=page_count, warnings=warnings,
        )
        try:
            ingest_document(
                conn, store, project_id=proj_id, actor_id=actor_id, logical_id=logical_id,
                data=data, inspection=db_inspection, pages=[], operation_id=op_intent_id,
            )
        except IngestRejected:
            pass
        if op_intent_id is not None:
            _save_result(conn, proj_id, op_intent_id, status, error)
        return status, {"error": {**error, "request_id": ctx.request_id}}

    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
        # The first check ran before the body was read, possibly long ago for
        # a slow client: authorize again at the moment of writing.
        _authorize_upload(conn, proj_id, actor_id)
        if idempotency_key:
            # Transaction-scoped advisory lock on (project_id, idempotency_key) held until commit/abort
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"idempotency:{proj_id}:{idempotency_key}",),
            )
            existing = conn.execute(
                """
                SELECT ik.request_hash, ik.operation_id, op.status
                FROM idempotency_keys ik
                JOIN operations op ON op.project_id = ik.project_id AND op.id = ik.operation_id
                WHERE ik.project_id = %s AND ik.idempotency_scope = 'document.import' AND ik.idempotency_key = %s
                """,
                (proj_id, idempotency_key),
            ).fetchone()

            if existing is not None:
                existing_hash, existing_op, op_status = existing
                if existing_hash != request_hash:
                    raise ApiError(409, "conflict", "idempotency key already used with different request")

                if op_status == "succeeded":
                    stored_ok = conn.execute(
                        "SELECT http_status, response_body FROM operation_results "
                        "WHERE project_id = %s AND operation_id = %s",
                        (proj_id, existing_op),
                    ).fetchone()
                    if stored_ok is not None:
                        return _replay_body(stored_ok[0], stored_ok[1], ctx.request_id)
                    # Operations from before 0009 have no stored response:
                    # rebuild the identity fields from the committed rows.
                    row = conn.execute(
                        """
                        SELECT dv.id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count,
                               rv.logical_id, rv.revision, a.size_bytes
                        FROM audit_events ae
                        JOIN document_versions dv ON dv.project_id = ae.project_id AND dv.resource_id = ae.resource_id
                        JOIN resource_versions rv ON rv.project_id = dv.project_id AND rv.id = dv.resource_id
                        JOIN artifacts a ON a.id = dv.artifact_id
                        WHERE ae.project_id = %s AND ae.operation_id = %s
                        """,
                        (proj_id, existing_op),
                    ).fetchone()
                    if row is not None:
                        doc_id, res_id, art_id, sha, page_cnt, log_id, rev, size_b = row
                        return 200, {
                            "id": str(doc_id),
                            "document_version_id": str(doc_id),
                            "project_id": str(proj_id),
                            "resource_id": str(res_id),
                            "artifact_id": str(art_id),
                            "logical_id": log_id,
                            "revision": rev,
                            "sha256": sha,
                            "size_bytes": size_b,
                            "page_count": page_cnt,
                            "replayed": True,
                        }
                    raise ApiError(500, "internal_error", "replayed document version not found")

                if op_status == "failed":
                    # Replay the response saved with the first attempt. It is
                    # never recomputed: the parser, limits or code may differ
                    # now, and re-inspecting would repeat the CPU cost.
                    stored = conn.execute(
                        "SELECT http_status, response_body FROM operation_results "
                        "WHERE project_id = %s AND operation_id = %s",
                        (proj_id, existing_op),
                    ).fetchone()
                    if stored is None:
                        raise ApiError(
                            409, "conflict",
                            "idempotency key was used for a failed operation whose original response was not retained",
                        )
                    return _replay_body(stored[0], stored[1], ctx.request_id)

                # If op_status is 'intent' (concurrent request still running), fail closed
                raise ApiError(409, "conflict", "idempotency key operation currently in progress")

            op_intent_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'document.import', %s, 'intent')",
                (op_intent_id, proj_id, actor_id),
            )
            conn.execute(
                "INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id) "
                "VALUES (%s, 'document.import', %s, %s, %s)",
                (proj_id, idempotency_key, request_hash, op_intent_id),
            )

        # Untrusted PDF structure is parsed only in the resource-limited isolation child
        # (RLIMIT memory/CPU, wall deadline, PDFium page-count cross-check), never in this
        # process. Any pypdf warning (for example a repaired xref) is an invalid PDF there.
        inspection, page_infos = inspect_pdf_isolated(
            data, Limits(max_bytes=ctx.max_bytes, max_pages=ctx.max_pages),
            cancelled=ctx.cancel.is_set if ctx.cancel is not None else None)

        if inspection.reason == "inspection_cancelled":
            # The client went away: abort and roll back. Nothing is persisted, in particular no
            # failed operation, so a retry with the same Idempotency-Key is not answered with
            # a cached failure.
            raise ApiError(503, "request_cancelled", "request cancelled before inspection finished", retryable=True)

        if inspection.outcome == "reject":
            return reject(
                inspection.reason or "", sha256=inspection.sha256, size_bytes=inspection.size_bytes,
                page_count=inspection.page_count, warnings=inspection.warnings,
                db_reason=inspection.reason,
            )

        if ctx.cancel is not None and ctx.cancel.is_set():
            # Disconnected after a good inspection but before anything was written: stop here.
            # (After this point the write is one short transaction that is allowed to commit;
            # a retry with the same Idempotency-Key then replays it.)
            raise ApiError(503, "request_cancelled", "request cancelled before the document was stored", retryable=True)

        pages = [
            PageGeometry(
                page_index=p.page_index,
                page_label=p.page_label,
                rotation=p.rotation,
                display_width_pt=p.width_pt,
                display_height_pt=p.height_pt,
            )
            for p in page_infos
        ]

        db_inspection = Inspection(
            outcome=inspection.outcome,
            reason=inspection.reason,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            page_count=inspection.page_count,
            warnings=inspection.warnings,
        )

        doc_version_id = ingest_document(
            conn, store, project_id=proj_id, actor_id=actor_id, logical_id=logical_id,
            data=data, inspection=db_inspection, pages=pages, operation_id=op_intent_id,
        )

        row = conn.execute(
            """
            SELECT dv.resource_id, dv.artifact_id, rv.revision
            FROM document_versions dv
            JOIN resource_versions rv ON rv.id = dv.resource_id
            WHERE dv.id = %s
            """,
            (doc_version_id,),
        ).fetchone()
        if row is None:
            raise ApiError(500, "internal_error", "document version was not persisted")

        resource_id, artifact_id, revision = row

        created = {
            "id": str(doc_version_id),
            "document_version_id": str(doc_version_id),
            "project_id": str(proj_id),
            "resource_id": str(resource_id),
            "artifact_id": str(artifact_id),
            "logical_id": logical_id,
            "revision": revision,
            "sha256": inspection.sha256,
            "size_bytes": inspection.size_bytes,
            "page_count": inspection.page_count,
            "flagged": inspection.outcome == "flag",
            "warnings": list(inspection.warnings),
        }
        if op_intent_id is not None:
            _save_result(conn, proj_id, op_intent_id, 201, created)
        return 201, created


async def upload_document_handler(
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
    project_id: Optional[uuid.UUID] = None,
) -> JSONResponse:
    """Authenticated and authorized PDF upload endpoint."""
    proj_id = resolve_project_id(request, project_id)
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        if not (1 <= len(idempotency_key) <= 256):
            raise ApiError(400, "bad_request", "invalid idempotency key format")
    max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    max_pages = getattr(request.app.state, "max_pdf_pages", DEFAULT_MAX_PDF_PAGES)
    ctx = _UploadContext(
        project_id=proj_id,
        actor_id=actor_id,
        max_bytes=max_bytes,
        max_pages=max_pages,
        idempotency_key=idempotency_key or None,
        request_id=request.headers.get("X-Request-Id", "req-1"),
        logical_hint=request.query_params.get("logical_id") or request.headers.get("X-Logical-Id"),
        cancel=threading.Event(),
    )

    # Authorize before a single body byte is read or a slot is taken (and again
    # inside the write transaction, see _upload_core).
    await _run_blocking(request, "meta", _authorize_upload, conn, proj_id, actor_id)

    slot = _take_slot(request, "upload", DEFAULT_UPLOAD_CONCURRENCY, "uploads")
    try:
        raw = await _read_upload_within_deadline(request, max_bytes)
        status, content = await _run_blocking(request, "upload", _upload_core, conn, store, ctx, raw,
                                              cancel=ctx.cancel)
    finally:
        slot.release()
    return JSONResponse(status_code=status, content=content)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _ReadableDocument:
    document_id: uuid.UUID
    project_id: uuid.UUID
    resource_id: uuid.UUID
    artifact_id: uuid.UUID
    doc_sha: str
    page_count: int
    logical_id: str
    revision: int
    size_bytes: int
    object_key: str


def _readable_document(
    conn: psycopg.Connection, actor_id: uuid.UUID, document_id: uuid.UUID, server_max_bytes: int
) -> _ReadableDocument:
    """Blocking: look up a document version and check that the actor may read
    its original and that its artifact is committed and consistent. Touches
    no artifact bytes."""
    row = conn.execute(
        """
        SELECT dv.id, dv.project_id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count,
               rv.logical_id, rv.revision, a.sha256, a.size_bytes, a.object_key, a.state::text
        FROM document_versions dv
        JOIN resource_versions rv ON rv.id = dv.resource_id
        JOIN artifacts a ON a.id = dv.artifact_id
        WHERE dv.id = %s
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "document not found")

    (doc_id, project_id, res_id, art_id, doc_sha, page_cnt, logical_id, rev,
     art_sha, size_bytes, obj_key, art_state) = row

    try:
        decision = authorize(conn, project_id, actor_id, "read_licensed_pdf_and_public_questions")
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "document not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    if art_state != "committed":
        raise ApiError(404, "not_found", "document artifact not committed")
    if art_sha != doc_sha:
        raise ApiError(500, "storage_corruption", "document and artifact checksum mismatch")
    if not isinstance(size_bytes, int) or size_bytes < 0 or size_bytes > server_max_bytes:
        raise ApiError(500, "storage_corruption", "invalid artifact size in database")

    return _ReadableDocument(
        document_id=doc_id, project_id=project_id, resource_id=res_id, artifact_id=art_id,
        doc_sha=doc_sha, page_count=page_cnt, logical_id=logical_id, revision=rev,
        size_bytes=size_bytes, object_key=obj_key,
    )


def _read_artifact(store: LocalArtifactStore, doc: _ReadableDocument, server_max_bytes: int) -> bytes:
    """Blocking: read the original under the size ceiling and verify it."""
    try:
        pdf_bytes = store.open(doc.object_key, expected_size=doc.size_bytes, max_bytes=server_max_bytes)
    except StorageCorruption as exc:
        raise ApiError(500, "storage_corruption", f"artifact corrupted: {exc}")
    except Exception as exc:
        raise ApiError(500, "storage_error", f"failed to open artifact ({type(exc).__name__})")

    if len(pdf_bytes) != doc.size_bytes or hashlib.sha256(pdf_bytes).hexdigest() != doc.doc_sha:
        raise ApiError(500, "storage_corruption", "artifact checksum or size mismatch")
    return pdf_bytes


class DownloadAdmissionMiddleware:
    """Release download admission even if response handoff or sending fails."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        callbacks = []
        scope["lab.download_release_callbacks"] = callbacks
        try:
            await self.app(scope, receive, send)
        finally:
            for release in callbacks:
                release()


async def _pdf_response(
    request: Request, store: LocalArtifactStore, doc: _ReadableDocument, server_max_bytes: int
) -> Response:
    slot = _take_slot(request, "download", DEFAULT_DOWNLOAD_CONCURRENCY, "downloads")
    try:
        pdf_bytes = await _run_blocking(request, "download", _read_artifact, store, doc, server_max_bytes)
        response = Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Length": str(len(pdf_bytes)),
                "Content-Disposition": f'attachment; filename="{doc.document_id}.pdf"',
            },
        )
        request.scope["lab.download_release_callbacks"].append(slot.release)
        return response
    except BaseException:
        slot.release()
        raise


async def download_document_handler(
    document_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> Response:
    """Authorized project-scoped original PDF download endpoint."""
    server_max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    doc = await _run_blocking(request, "meta", _readable_document, conn, actor_id, document_id, server_max_bytes)
    return await _pdf_response(request, store, doc, server_max_bytes)


async def get_document_handler(
    document_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> Response:
    """Get document version metadata, or download if Accept: application/pdf."""
    server_max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    doc = await _run_blocking(request, "meta", _readable_document, conn, actor_id, document_id, server_max_bytes)

    if "application/pdf" in request.headers.get("Accept", ""):
        return await _pdf_response(request, store, doc, server_max_bytes)

    return JSONResponse(
        status_code=200,
        content={
            "id": str(doc.document_id),
            "document_version_id": str(doc.document_id),
            "project_id": str(doc.project_id),
            "resource_id": str(doc.resource_id),
            "artifact_id": str(doc.artifact_id),
            "logical_id": doc.logical_id,
            "revision": doc.revision,
            "sha256": doc.doc_sha,
            "size_bytes": doc.size_bytes,
            "page_count": doc.page_count,
        },
    )


def _get_page(conn: psycopg.Connection, actor_id: uuid.UUID, document_id: uuid.UUID, page_index: int) -> dict:
    doc_row = conn.execute(
        "SELECT project_id, page_count FROM document_versions WHERE id = %s",
        (document_id,),
    ).fetchone()
    if doc_row is None:
        raise ApiError(404, "not_found", "document not found")

    project_id, page_count = doc_row

    try:
        decision = authorize(conn, project_id, actor_id, "read_licensed_pdf_and_public_questions")
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "document not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    if page_index < 0 or page_index >= page_count:
        raise ApiError(404, "not_found", f"page_index {page_index} out of range")

    page_row = conn.execute(
        """
        SELECT page_index, page_label, rotation, width_pt, height_pt
        FROM pages
        WHERE document_version_id = %s AND page_index = %s
        """,
        (document_id, page_index),
    ).fetchone()
    if page_row is None:
        raise ApiError(404, "not_found", f"page {page_index} not found")

    idx, label, rot, width, height = page_row
    return {
        "document_version_id": str(document_id),
        "page_index": idx,
        "page_label": label,
        "rotation": rot,
        "width_pt": float(width),
        "height_pt": float(height),
    }


def _prefers_png_preview(accept_header: str) -> bool:
    """Parse HTTP Accept header with bounded media-range and quality weights.

    Preserves default JSON when JSON is preferred or equal quality; returns True
    only when image/png is explicitly acceptable with higher quality than JSON.
    """
    if not accept_header or not isinstance(accept_header, str):
        return False

    raw = accept_header[:1024].strip()
    if not raw:
        return False

    png_q = 0.0
    json_q = 0.0

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = [t.strip() for t in part.split(";")]
        media_type = tokens[0].lower()

        q_val = 1.0
        for param in tokens[1:]:
            if param.lower().startswith("q="):
                try:
                    q_val = float(param[2:].strip())
                    if not (0.0 <= q_val <= 1.0):
                        q_val = 0.0
                except (ValueError, TypeError):
                    q_val = 0.0

        if media_type == "image/png":
            png_q = max(png_q, q_val)
        elif media_type in ("application/json", "text/json"):
            json_q = max(json_q, q_val)

    return png_q > 0.0 and png_q > json_q


async def _preview_response(
    request: Request,
    conn: psycopg.Connection,
    actor_id: uuid.UUID,
    document_id: uuid.UUID,
    page_index: int,
    server_max_bytes: int,
) -> Response:
    """Authorized PDF page preview rendering at fixed 150 DPI in an isolated subprocess."""
    # 1. Authorize document access and validate committed artifact state BEFORE slot or render
    doc = await _run_blocking(request, "meta", _readable_document, conn, actor_id, document_id, server_max_bytes)
    if page_index < 0 or page_index >= doc.page_count:
        raise ApiError(404, "not_found", f"page_index {page_index} out of range")

    # 2. Acquire admission slot BEFORE PDF read
    slot = _take_slot(request, "preview", DEFAULT_PREVIEW_CONCURRENCY, "page previews")
    try:
        store = await _run_blocking(request, "preview", get_store, request)

        # 3. Read artifact bytes under the preview pool
        pdf_bytes = await _run_blocking(request, "preview", _read_artifact, store, doc, server_max_bytes)

        # 4. Supervised subprocess preview rendering under the preview pool
        timeout_s = getattr(request.app.state, "preview_timeout_s", DEFAULT_PREVIEW_TIMEOUT_S)
        try:
            asset = await _run_blocking(
                request,
                "preview",
                render_preview_in_subprocess,
                pdf_bytes,
                page_index,
                timeout_s,
            )
        except PreviewError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message, retryable=exc.retryable) from exc

        response = Response(
            content=asset.png,
            media_type="image/png",
            headers={
                "Content-Length": str(len(asset.png)),
                "Cache-Control": "no-store",
                "Vary": "Accept",
                "ETag": f'"{asset.sha256}"',
                "X-Document-Sha256": doc.doc_sha,
                "X-Page-Index": str(page_index),
                "X-Image-Sha256": asset.sha256,
                "X-Image-Width": str(asset.width_px),
                "X-Image-Height": str(asset.height_px),
            },
        )
        request.scope["lab.download_release_callbacks"].append(slot.release)
        return response
    except BaseException:
        slot.release()
        raise


async def get_page_handler(
    document_id: uuid.UUID,
    page_index: int,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> Response:
    """Get per-page normalized geometry (JSON) or rendered page preview (Accept: image/png)."""
    if _prefers_png_preview(request.headers.get("Accept", "")):
        server_max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
        return await _preview_response(
            request, conn, actor_id, document_id, page_index, server_max_bytes
        )

    content = await _run_blocking(request, "meta", _get_page, conn, actor_id, document_id, page_index)
    return JSONResponse(
        status_code=200,
        content=content,
        headers={"Vary": "Accept", "Cache-Control": "no-store"},
    )


PAGE_RESPONSES = {
    200: {
        "description": "Normalized page geometry (JSON) or rendered page preview (PNG).",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "document_version_id": {"type": "string", "format": "uuid"},
                        "page_index": {"type": "integer"},
                        "page_label": {"type": "string", "nullable": True},
                        "rotation": {"type": "integer"},
                        "width_pt": {"type": "number"},
                        "height_pt": {"type": "number"},
                    },
                    "required": ["document_version_id", "page_index", "page_label", "rotation", "width_pt", "height_pt"],
                }
            },
            "image/png": {
                "schema": {"type": "string", "format": "binary"},
                "headers": {
                    "X-Document-Sha256": {"schema": {"type": "string"}, "description": "Actual document SHA-256"},
                    "X-Page-Index": {"schema": {"type": "integer"}, "description": "0-based page index"},
                    "X-Image-Sha256": {"schema": {"type": "string"}, "description": "Actual rendered PNG SHA-256"},
                    "X-Image-Width": {"schema": {"type": "integer"}, "description": "Actual PNG width in pixels"},
                    "X-Image-Height": {"schema": {"type": "integer"}, "description": "Actual PNG height in pixels"},
                    "Cache-Control": {"schema": {"type": "string"}, "description": "no-store"},
                },
            },
        },
    },
    403: {"description": "Observer or cross-project permission denied."},
    404: {"description": "Document not found or page index out of range."},
    422: {"description": "Malformed path or render pixel limit exceeded."},
    503: {"description": "Server busy or renderer temporarily unavailable."},
    504: {"description": "Page rendering timed out."},
}

# Response headers belong to the response object, not a media type object.
PAGE_RESPONSES[200]["headers"] = PAGE_RESPONSES[200]["content"]["image/png"].pop("headers")

# Route registrations
router.add_api_route(
    "/api/v1/documents",
    upload_document_handler,
    methods=["POST"],
    name="upload_document_v1",
)
router.add_api_route(
    "/documents",
    upload_document_handler,
    methods=["POST"],
    name="upload_document_compat",
)
router.add_api_route(
    "/api/v1/projects/{project_id}/documents",
    upload_document_handler,
    methods=["POST"],
    name="upload_project_document",
)

router.add_api_route(
    "/api/v1/documents/{document_id}/original",
    download_document_handler,
    methods=["GET"],
    name="download_document_original_v1",
)
router.add_api_route(
    "/documents/{document_id}/original",
    download_document_handler,
    methods=["GET"],
    name="download_document_original_compat",
)
router.add_api_route(
    "/api/v1/documents/{document_id}/download",
    download_document_handler,
    methods=["GET"],
    name="download_document_v1",
)
router.add_api_route(
    "/documents/{document_id}/download",
    download_document_handler,
    methods=["GET"],
    name="download_document_compat",
)

router.add_api_route(
    "/api/v1/documents/{document_id}",
    get_document_handler,
    methods=["GET"],
    name="get_document_v1",
)
router.add_api_route(
    "/documents/{document_id}",
    get_document_handler,
    methods=["GET"],
    name="get_document_compat",
)

router.add_api_route(
    "/api/v1/documents/{document_id}/pages/{page_index}",
    get_page_handler,
    methods=["GET"],
    name="get_document_page_v1",
    responses=PAGE_RESPONSES,
)
router.add_api_route(
    "/documents/{document_id}/pages/{page_index}",
    get_page_handler,
    methods=["GET"],
    name="get_document_page_compat",
    responses=PAGE_RESPONSES,
)
