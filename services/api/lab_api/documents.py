"""Document ingestion, versioning, and download endpoints (SPEC R01, 8, 10.1-10.2, 16.4)."""
from __future__ import annotations

import email
import email.policy
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _sub in (_REPO_ROOT / "db", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from lab_db.ingest import IngestRejected, Inspection, PageGeometry, ingest_document
from lab_db.repo import NotFound, PermissionDenied, authorize
from lab_db.storage import LocalArtifactStore, StorageCorruption
from lab_parser.importer import Limits, inspect_pdf
from lab_parser.pages import extract_pages

from .auth import AuthError, get_signing_secret, verify_actor_token
from .config import Settings
from .pdf_body import (
    DEFAULT_MAX_PDF_BYTES,
    PdfRequestBodyError,
    read_pdf_request_body,
)
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


def _make_rejection_error_response(
    reason: str,
    *,
    request_id: str = "req-1",
    sha256: str = "",
    size_bytes: int = 0,
    page_count: Optional[int] = None,
    error_type: Optional[str] = None,
) -> JSONResponse:
    if reason == "invalid_page_geometry":
        return ApiError(
            status_code=422,
            code="invalid_page_geometry",
            message="failed to parse page geometry from PDF",
            details={"error": error_type or "ValueError"},
        ).to_error_response(request_id)

    code, status_code = REJECTION_CODE_MAP.get(reason, ("invalid_pdf", 422))
    return ApiError(
        status_code=status_code,
        code=code,
        message=f"document rejected: {reason}",
        details={
            "reason": reason,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "page_count": page_count,
        },
    ).to_error_response(request_id)


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

    try:
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
    except psycopg.Error:
        pass

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


async def extract_upload_data(request: Request, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Extract PDF bytes and optional logical_id from bounded streaming multipart or raw PDF body."""
    raw_ct = request.headers.get("Content-Type", "").strip()
    ct_lower = raw_ct.lower()

    if ct_lower.startswith("application/pdf"):
        data = await read_pdf_request_body(request, max_bytes=max_bytes)
        return data, None

    if ct_lower.startswith("multipart/form-data"):
        max_multipart_bytes = max_bytes + 65536
        cl_header = request.headers.get("Content-Length")
        if cl_header:
            try:
                cl = int(cl_header.strip())
            except ValueError:
                raise ApiError(400, "invalid_content_length", "invalid Content-Length header")
            if cl < 0:
                raise ApiError(400, "invalid_content_length", "negative Content-Length header")
            if cl > max_multipart_bytes:
                raise ApiError(413, "payload_too_large", "request body exceeds size limit")

        # Stream chunks with strict byte limit to prevent unbounded memory buffering
        chunks = []
        total_bytes = 0
        async for chunk in request.stream():
            if chunk:
                total_bytes += len(chunk)
                if total_bytes > max_multipart_bytes:
                    raise ApiError(413, "payload_too_large", "request body exceeds size limit")
                chunks.append(chunk)

        raw_body = b"".join(chunks)

        try:
            header_bytes = f"Content-Type: {raw_ct}\r\n\r\n".encode("latin-1")
        except UnicodeEncodeError:
            raise ApiError(400, "invalid_request", "invalid characters in Content-Type header")

        try:
            msg = email.message_from_bytes(header_bytes + raw_body, policy=email.policy.default)
        except Exception as exc:
            raise ApiError(400, "invalid_request", f"malformed multipart body ({type(exc).__name__})")

        if not msg.is_multipart():
            raise ApiError(400, "invalid_request", "expected multipart body")

        pdf_bytes: Optional[bytes] = None
        form_logical_id: Optional[str] = None

        for part in msg.iter_parts():
            part_ct = part.get_content_type()
            part_name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()

            if part_name == "logical_id":
                try:
                    form_logical_id = part.get_content().strip()
                except Exception:
                    pass
            elif part_name in ("file", "pdf", "document") or part_ct == "application/pdf":
                is_pdf_type = (part_ct == "application/pdf") or (
                    part_ct == "application/octet-stream"
                    and bool(filename and filename.lower().endswith(".pdf"))
                )
                if not is_pdf_type:
                    raise ApiError(
                        415,
                        "unsupported_media_type",
                        f"unsupported media type '{part_ct}' for PDF upload",
                    )
                if pdf_bytes is not None:
                    raise ApiError(
                        400,
                        "invalid_request",
                        "multiple PDF parts in multipart request",
                    )
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    raise ApiError(400, "invalid_request", "failed to decode PDF part")
                if len(payload) > max_bytes:
                    raise ApiError(413, "payload_too_large", "PDF file exceeds size limit")
                pdf_bytes = payload

        if pdf_bytes is None:
            raise ApiError(400, "invalid_request", "multipart body does not contain a PDF file")

        return pdf_bytes, form_logical_id

    raise ApiError(415, "unsupported_media_type", "Content-Type must be application/pdf or multipart/form-data")


async def upload_document_handler(
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
    project_id: Optional[uuid.UUID] = None,
) -> JSONResponse:
    """Authenticated and authorized PDF upload endpoint."""
    proj_id = resolve_project_id(request, project_id)

    try:
        authorize(conn, proj_id, actor_id, "import_and_configure_parse")
    except NotFound:
        raise ApiError(404, "not_found", "project not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    max_pages = getattr(request.app.state, "max_pdf_pages", DEFAULT_MAX_PDF_PAGES)

    try:
        data, form_logical_id = await extract_upload_data(request, max_bytes=max_bytes)
    except PdfRequestBodyError as exc:
        raise ApiError(status_code=exc.status_code, code=exc.code, message=exc.message) from exc

    logical_id = resolve_logical_id(request, form_logical_id)
    pdf_sha256 = hashlib.sha256(data).hexdigest()
    request_hash = hashlib.sha256(f"{logical_id}:{pdf_sha256}".encode("utf-8")).hexdigest()

    idempotency_key = request.headers.get("Idempotency-Key")
    op_intent_id: Optional[uuid.UUID] = None

    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        if not (1 <= len(idempotency_key) <= 256):
            raise ApiError(400, "bad_request", "invalid idempotency key format")

    with conn.transaction():
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
                        return JSONResponse(
                            status_code=200,
                            content={
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
                            },
                        )
                    raise ApiError(500, "internal_error", "replayed document version not found")

                if op_status == "failed":
                    audit_row = conn.execute(
                        """
                        SELECT reason
                        FROM audit_events
                        WHERE project_id = %s AND operation_id = %s AND action = 'document.rejected'
                        """,
                        (proj_id, existing_op),
                    ).fetchone()
                    if audit_row is not None:
                        rej_reason = audit_row[0] or ""
                        req_id = request.headers.get("X-Request-Id", "req-1")
                        if rej_reason == "invalid_page_geometry":
                            try:
                                extract_pages(data)
                                err_name = "ValueError"
                            except Exception as exc:
                                err_name = type(exc).__name__
                            return _make_rejection_error_response(
                                "invalid_page_geometry",
                                request_id=req_id,
                                error_type=err_name,
                            )
                        inspection = inspect_pdf(data, limits=Limits(max_bytes=max_bytes, max_pages=max_pages))
                        return _make_rejection_error_response(
                            rej_reason or inspection.reason or "",
                            request_id=req_id,
                            sha256=inspection.sha256,
                            size_bytes=inspection.size_bytes,
                            page_count=inspection.page_count,
                        )
                    raise ApiError(409, "conflict", "idempotency key was used for a failed operation")

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

        inspection = inspect_pdf(data, limits=Limits(max_bytes=max_bytes, max_pages=max_pages))

        if inspection.outcome == "reject":
            db_inspection = Inspection(
                outcome="reject",
                reason=inspection.reason,
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                page_count=inspection.page_count,
                warnings=inspection.warnings,
            )
            try:
                ingest_document(
                    conn,
                    store,
                    project_id=proj_id,
                    actor_id=actor_id,
                    logical_id=logical_id,
                    data=data,
                    inspection=db_inspection,
                    pages=[],
                    operation_id=op_intent_id,
                )
            except IngestRejected:
                pass

            return _make_rejection_error_response(
                inspection.reason or "",
                request_id=request.headers.get("X-Request-Id", "req-1"),
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                page_count=inspection.page_count,
            )

        try:
            page_infos = extract_pages(data)
        except Exception as exc:
            db_inspection = Inspection(
                outcome="reject",
                reason="invalid_page_geometry",
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                page_count=inspection.page_count,
                warnings=inspection.warnings,
            )
            try:
                ingest_document(
                    conn,
                    store,
                    project_id=proj_id,
                    actor_id=actor_id,
                    logical_id=logical_id,
                    data=data,
                    inspection=db_inspection,
                    pages=[],
                    operation_id=op_intent_id,
                )
            except IngestRejected:
                pass

            return _make_rejection_error_response(
                "invalid_page_geometry",
                request_id=request.headers.get("X-Request-Id", "req-1"),
                error_type=type(exc).__name__,
            )

        pages = [
            PageGeometry(
                page_index=p.page_index,
                page_label=p.page_label,
                rotation=p.rotation,
                display_width_pt=p.display_width_pt,
                display_height_pt=p.display_height_pt,
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
            conn,
            store,
            project_id=proj_id,
            actor_id=actor_id,
            logical_id=logical_id,
            data=data,
            inspection=db_inspection,
            pages=pages,
            operation_id=op_intent_id,
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

        return JSONResponse(
            status_code=201,
            content={
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
            },
        )


async def download_document_handler(
    document_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> Response:
    """Authorized project-scoped original PDF download endpoint."""
    row = conn.execute(
        """
        SELECT dv.project_id, dv.sha256, a.sha256, a.object_key, a.size_bytes, a.state::text
        FROM document_versions dv
        JOIN artifacts a ON a.id = dv.artifact_id
        WHERE dv.id = %s
        """,
        (document_id,),
    ).fetchone()

    if row is None:
        raise ApiError(404, "not_found", "document not found")

    project_id, doc_sha, art_sha, object_key, size_bytes, artifact_state = row

    try:
        decision = authorize(
            conn, project_id, actor_id, "read_licensed_pdf_and_public_questions"
        )
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "document not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    if artifact_state != "committed":
        raise ApiError(404, "not_found", "document artifact not committed")

    if art_sha != doc_sha:
        raise ApiError(500, "storage_corruption", "document and artifact checksum mismatch")

    server_max_bytes = getattr(
        request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES
    )

    if (
        not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes > server_max_bytes
    ):
        raise ApiError(500, "storage_corruption", "invalid artifact size in database")

    try:
        pdf_bytes = store.open(
            object_key,
            expected_size=size_bytes,
            max_bytes=server_max_bytes,
        )
    except StorageCorruption as exc:
        raise ApiError(500, "storage_corruption", f"artifact corrupted: {exc}")
    except Exception as exc:
        raise ApiError(
            500, "storage_error", f"failed to open artifact ({type(exc).__name__})"
        )

    if len(pdf_bytes) != size_bytes or hashlib.sha256(pdf_bytes).hexdigest() != doc_sha:
        raise ApiError(500, "storage_corruption", "artifact checksum or size mismatch")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Length": str(len(pdf_bytes)),
            "Content-Disposition": f'attachment; filename="{document_id}.pdf"',
        },
    )


async def get_document_handler(
    document_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> Response:
    """Get document version metadata, or download if Accept: application/pdf."""
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

    (
        doc_id,
        project_id,
        res_id,
        art_id,
        doc_sha,
        page_cnt,
        logical_id,
        rev,
        art_sha,
        size_bytes,
        obj_key,
        art_state,
    ) = row

    try:
        decision = authorize(
            conn, project_id, actor_id, "read_licensed_pdf_and_public_questions"
        )
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

    server_max_bytes = getattr(
        request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES
    )

    if (
        not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes > server_max_bytes
    ):
        raise ApiError(500, "storage_corruption", "invalid artifact size in database")

    accept = request.headers.get("Accept", "")
    if "application/pdf" in accept:
        try:
            pdf_bytes = store.open(
                obj_key,
                expected_size=size_bytes,
                max_bytes=server_max_bytes,
            )
        except StorageCorruption as exc:
            raise ApiError(500, "storage_corruption", f"artifact corrupted: {exc}")
        except Exception as exc:
            raise ApiError(
                500, "storage_error", f"failed to open artifact ({type(exc).__name__})"
            )

        if len(pdf_bytes) != size_bytes or hashlib.sha256(pdf_bytes).hexdigest() != doc_sha:
            raise ApiError(500, "storage_corruption", "artifact checksum or size mismatch")

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Length": str(len(pdf_bytes)),
                "Content-Disposition": f'attachment; filename="{document_id}.pdf"',
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "id": str(doc_id),
            "document_version_id": str(doc_id),
            "project_id": str(project_id),
            "resource_id": str(res_id),
            "artifact_id": str(art_id),
            "logical_id": logical_id,
            "revision": rev,
            "sha256": doc_sha,
            "size_bytes": size_bytes,
            "page_count": page_cnt,
        },
    )


async def get_page_handler(
    document_id: uuid.UUID,
    page_index: int,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    """Get per-page normalized geometry and dimensions (SPEC 8)."""
    doc_row = conn.execute(
        "SELECT project_id, page_count FROM document_versions WHERE id = %s",
        (document_id,),
    ).fetchone()

    if doc_row is None:
        raise ApiError(404, "not_found", "document not found")

    project_id, page_count = doc_row

    try:
        decision = authorize(
            conn, project_id, actor_id, "read_licensed_pdf_and_public_questions"
        )
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
    return JSONResponse(
        status_code=200,
        content={
            "document_version_id": str(document_id),
            "page_index": idx,
            "page_label": label,
            "rotation": rot,
            "width_pt": float(width),
            "height_pt": float(height),
        },
    )


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
)
router.add_api_route(
    "/documents/{document_id}/pages/{page_index}",
    get_page_handler,
    methods=["GET"],
    name="get_document_page_compat",
)
