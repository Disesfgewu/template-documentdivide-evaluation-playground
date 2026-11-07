"""Document ingestion, versioning, and download endpoints (SPEC R01, 8, 10.1-10.2, 16.4)."""
from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

# Ensure sibling directories (db, services/parser) are in sys.path when running from services/api
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _sub in (_REPO_ROOT / "db", _REPO_ROOT / "services" / "parser"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from lab_db.ingest import IngestRejected, Inspection, PageGeometry, ingest_document
from lab_db.repo import NotFound, PermissionDenied, authorize
from lab_db.storage import LocalArtifactStore
from lab_parser.importer import Limits, inspect_pdf
from lab_parser.pages import extract_pages

from .auth import get_signing_secret, verify_actor_token
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


def make_error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str = "req-1",
    details: Optional[dict[str, Any]] = None,
    retryable: bool = False,
) -> JSONResponse:
    content = {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
            "details": details or {},
            "retry_after": None,
        }
    }
    return JSONResponse(status_code=status_code, content=content)


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
    secret = get_signing_secret(settings)
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

    # Establish session-level actor context for RLS policies and audit triggers
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


def resolve_logical_id(request: Request) -> str:
    """Resolve logical_id from query parameter or X-Logical-Id header; defaults to 'default'."""
    val = request.query_params.get("logical_id") or request.headers.get("X-Logical-Id")
    if not val:
        return "default"
    if not (1 <= len(val) <= 128):
        raise ApiError(
            400, "bad_request", "logical_id length must be between 1 and 128 characters"
        )
    return val


router = APIRouter()


async def upload_document_handler(
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    store: LocalArtifactStore = Depends(get_store),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
    project_id: Optional[uuid.UUID] = None,
) -> JSONResponse:
    """Authenticated and authorized PDF upload endpoint.

    Streams and bounds PDF request body using server-owned limits, executes
    strict PDF inspection, records rejections as audit events, and writes
    accepted documents to artifact storage and PostgreSQL atomically with lineage.
    """
    proj_id = resolve_project_id(request, project_id)

    # Verify project-level authorization (SPEC 16.4: import_and_configure_parse)
    try:
        authorize(conn, proj_id, actor_id, "import_and_configure_parse")
    except NotFound:
        raise ApiError(404, "not_found", "project not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    # Server-owned limits: caller cannot raise limits via query parameters
    max_bytes = getattr(request.app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    max_pages = getattr(request.app.state, "max_pdf_pages", DEFAULT_MAX_PDF_PAGES)

    # Enforce streaming byte limits, Content-Type, and Content-Length
    try:
        data = await read_pdf_request_body(request, max_bytes=max_bytes)
    except PdfRequestBodyError as exc:
        raise ApiError(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
        ) from exc

    logical_id = resolve_logical_id(request)
    pdf_sha256 = hashlib.sha256(data).hexdigest()

    # Idempotency handling (SPEC 8 & 16.4)
    idempotency_key = request.headers.get("Idempotency-Key")
    op_intent_id: Optional[uuid.UUID] = None
    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        if not (1 <= len(idempotency_key) <= 256):
            raise ApiError(400, "bad_request", "invalid idempotency key format")

        # Transaction-scoped advisory lock on (project_id, idempotency_key)
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
            if existing_hash != pdf_sha256:
                raise ApiError(
                    409,
                    "conflict",
                    "idempotency key already used with different request",
                )

            if op_status == "succeeded":
                # Replay previous result tied directly to this operation
                row = conn.execute(
                    """
                    SELECT dv.id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count,
                           rv.logical_id, rv.revision
                    FROM audit_events ae
                    JOIN document_versions dv ON dv.project_id = ae.project_id AND dv.resource_id = ae.resource_id
                    JOIN resource_versions rv ON rv.project_id = dv.project_id AND rv.id = dv.resource_id
                    WHERE ae.project_id = %s AND ae.operation_id = %s
                    """,
                    (proj_id, existing_op),
                ).fetchone()
                if row is not None:
                    doc_id, res_id, art_id, sha, page_cnt, log_id, rev = row
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
                            "page_count": page_cnt,
                            "replayed": True,
                        },
                    )

            if op_status == "failed":
                raise ApiError(
                    409,
                    "conflict",
                    "idempotency key was used for a failed operation",
                )

        # Claim the idempotency key with intent operation
        op_intent_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'document.import', %s, 'intent')",
            (op_intent_id, proj_id, actor_id),
        )
        conn.execute(
            "INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id) "
            "VALUES (%s, 'document.import', %s, %s, %s)",
            (proj_id, idempotency_key, pdf_sha256, op_intent_id),
        )

    # Strict PDF inspection with server-owned limits
    inspection = inspect_pdf(data, limits=Limits(max_bytes=max_bytes, max_pages=max_pages))

    if inspection.outcome == "reject":
        # Record failed operation and document.rejected audit event
        try:
            db_inspection = Inspection(
                outcome="reject",
                reason=inspection.reason,
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                page_count=inspection.page_count,
                warnings=inspection.warnings,
            )
            ingest_document(
                conn,
                store,
                project_id=proj_id,
                actor_id=actor_id,
                logical_id=logical_id,
                data=data,
                inspection=db_inspection,
                pages=[],
            )
        except IngestRejected:
            pass

        if op_intent_id is not None:
            conn.execute(
                "UPDATE operations SET status = 'failed', finished_at = now() WHERE id = %s",
                (op_intent_id,),
            )

        code_map = {
            "empty_file": ("empty_file", 422),
            "not_a_pdf": ("invalid_pdf", 422),
            "corrupt_pdf": ("corrupt_pdf", 422),
            "encrypted_pdf": ("unsupported_encryption", 422),
            "page_limit_exceeded": ("page_limit_exceeded", 422),
            "file_too_large": ("payload_too_large", 413),
        }
        code, status_code = code_map.get(inspection.reason or "", ("invalid_pdf", 422))
        raise ApiError(
            status_code=status_code,
            code=code,
            message=f"document rejected: {inspection.reason}",
            details={
                "reason": inspection.reason,
                "sha256": inspection.sha256,
                "size_bytes": inspection.size_bytes,
                "page_count": inspection.page_count,
            },
        )

    # Extract page geometry
    try:
        page_infos = extract_pages(data)
    except Exception as exc:
        try:
            db_inspection = Inspection(
                outcome="reject",
                reason="invalid_page_geometry",
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                page_count=inspection.page_count,
                warnings=inspection.warnings,
            )
            ingest_document(
                conn,
                store,
                project_id=proj_id,
                actor_id=actor_id,
                logical_id=logical_id,
                data=data,
                inspection=db_inspection,
                pages=[],
            )
        except IngestRejected:
            pass

        if op_intent_id is not None:
            conn.execute(
                "UPDATE operations SET status = 'failed', finished_at = now() WHERE id = %s",
                (op_intent_id,),
            )

        raise ApiError(
            status_code=422,
            code="invalid_page_geometry",
            message="failed to parse page geometry from PDF",
            details={"error": type(exc).__name__},
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

    # Tie idempotency claim to the actual operation created by record_change
    if idempotency_key and op_intent_id is not None:
        audit_row = conn.execute(
            """
            SELECT ae.operation_id
            FROM document_versions dv
            JOIN audit_events ae ON ae.project_id = dv.project_id AND ae.resource_id = dv.resource_id
            WHERE dv.id = %s
            """,
            (doc_version_id,),
        ).fetchone()
        if audit_row is not None:
            change_op_id = audit_row[0]
            conn.execute(
                """
                UPDATE idempotency_keys
                SET operation_id = %s
                WHERE project_id = %s AND idempotency_scope = 'document.import' AND idempotency_key = %s
                """,
                (change_op_id, proj_id, idempotency_key),
            )
            conn.execute(
                "DELETE FROM operations WHERE id = %s AND status = 'intent'",
                (op_intent_id,),
            )

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
        SELECT dv.project_id, dv.sha256, a.object_key, a.size_bytes
        FROM document_versions dv
        JOIN artifacts a ON a.id = dv.artifact_id
        WHERE dv.id = %s
        """,
        (document_id,),
    ).fetchone()

    if row is None:
        raise ApiError(404, "not_found", "document not found")

    project_id, sha256, object_key, size_bytes = row

    # Authorize caller in the document's project
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

    try:
        pdf_bytes = store.open(object_key)
    except Exception as exc:
        raise ApiError(
            500, "storage_error", f"failed to open artifact ({type(exc).__name__})"
        )

    actual_sha = hashlib.sha256(pdf_bytes).hexdigest()
    if actual_sha != sha256:
        raise ApiError(500, "storage_corruption", "artifact checksum mismatch")

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
               rv.logical_id, rv.revision, a.size_bytes, a.object_key
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
        sha,
        page_cnt,
        logical_id,
        rev,
        size_bytes,
        obj_key,
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

    accept = request.headers.get("Accept", "")
    if "application/pdf" in accept:
        try:
            pdf_bytes = store.open(obj_key)
        except Exception as exc:
            raise ApiError(
                500, "storage_error", f"failed to open artifact ({type(exc).__name__})"
            )
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
            "sha256": sha,
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
