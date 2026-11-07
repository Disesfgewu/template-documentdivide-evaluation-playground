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
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from lab_db.ingest import IngestRejected, Inspection, PageGeometry, ingest_document
from lab_db.repo import IdempotencyConflict, NotFound, PermissionDenied, authorize, claim_or_replay
from lab_db.storage import LocalArtifactStore
from lab_parser.importer import Limits, inspect_pdf
from lab_parser.pages import extract_pages

from .config import Settings
from .pdf_body import DEFAULT_MAX_PDF_BYTES, PdfRequestBodyError, read_pdf_request_body
from .readiness import connect


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
    """Authenticate the caller via Authorization: Bearer <actor_id> or X-Actor-Id.

    Validates that the actor exists in the PostgreSQL actors table.
    Rejects unauthenticated or invalid callers with 401.
    """
    auth_header = request.headers.get("Authorization")
    x_actor = request.headers.get("X-Actor-Id")

    raw_id: Optional[str] = None
    if auth_header:
        parts = auth_header.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_id = parts[1]
        elif len(parts) == 1:
            raw_id = parts[0]
        else:
            raise ApiError(
                status_code=401,
                code="unauthorized",
                message="invalid Authorization header format",
            )
    elif x_actor:
        raw_id = x_actor.strip()

    if not raw_id:
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="missing or invalid authentication",
        )

    try:
        actor_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        raise ApiError(
            status_code=401,
            code="unauthorized",
            message="invalid actor credentials",
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


def resolve_logical_id(request: Request) -> str:
    """Resolve logical_id from query parameter or X-Logical-Id header; defaults to 'default'."""
    val = request.query_params.get("logical_id") or request.headers.get("X-Logical-Id")
    if not val:
        return "default"
    if not (1 <= len(val) <= 128):
        raise ApiError(400, "bad_request", "logical_id length must be between 1 and 128 characters")
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

    Streams and bounds PDF request body, executes strict inspection,
    records rejections as audit events, and writes accepted documents to
    artifact storage and PostgreSQL atomically.
    """
    proj_id = resolve_project_id(request, project_id)

    # Verify project-level authorization (SPEC 16.4: import_and_configure_parse)
    try:
        authorize(conn, proj_id, actor_id, "import_and_configure_parse")
    except NotFound:
        raise ApiError(404, "not_found", "project not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    max_bytes = DEFAULT_MAX_PDF_BYTES
    if "max_bytes" in request.query_params:
        try:
            max_bytes = int(request.query_params["max_bytes"])
        except ValueError:
            raise ApiError(400, "bad_request", "invalid max_bytes parameter")

    # Enforce streaming byte limits, Content-Type, and Content-Length
    data = await read_pdf_request_body(request, max_bytes=max_bytes)
    logical_id = resolve_logical_id(request)
    pdf_sha256 = hashlib.sha256(data).hexdigest()

    # Idempotency support (SPEC 8 & 16.4)
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        try:
            claim = claim_or_replay(
                conn,
                proj_id,
                actor_id,
                scope="document.import",
                key=idempotency_key,
                request_hash=pdf_sha256,
                kind="document.import",
            )
        except IdempotencyConflict:
            raise ApiError(409, "conflict", "idempotency key already used with different request")

        if not claim.created:
            # Replay previous result
            row = conn.execute(
                """
                SELECT dv.id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count,
                       rv.logical_id, rv.revision
                FROM document_versions dv
                JOIN resource_versions rv ON rv.id = dv.resource_id
                WHERE dv.project_id = %s AND dv.sha256 = %s
                ORDER BY rv.revision DESC
                LIMIT 1
                """,
                (proj_id, pdf_sha256),
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

    max_pages = 2000
    if "max_pages" in request.query_params:
        try:
            max_pages = int(request.query_params["max_pages"])
        except ValueError:
            raise ApiError(400, "bad_request", "invalid max_pages parameter")

    # Strict PDF inspection
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
        decision = authorize(conn, project_id, actor_id, "read_licensed_pdf_and_public_questions")
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "document not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    try:
        pdf_bytes = store.open(object_key)
    except Exception as exc:
        raise ApiError(500, "storage_error", f"failed to open artifact ({type(exc).__name__})")

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
    accept = request.headers.get("accept", "").lower()
    if "application/pdf" in accept:
        return await download_document_handler(
            document_id=document_id,
            request=request,
            conn=conn,
            store=store,
            actor_id=actor_id,
        )

    row = conn.execute(
        """
        SELECT dv.id, dv.project_id, dv.resource_id, dv.artifact_id,
               dv.sha256, dv.page_count, rv.logical_id, rv.revision,
               rv.created_at, rv.created_by
        FROM document_versions dv
        JOIN resource_versions rv ON rv.id = dv.resource_id
        WHERE dv.id = %s
        """,
        (document_id,),
    ).fetchone()

    if row is None:
        raise ApiError(404, "not_found", "document not found")

    (
        doc_id,
        project_id,
        resource_id,
        artifact_id,
        sha256,
        page_count,
        logical_id,
        revision,
        created_at,
        created_by,
    ) = row

    try:
        decision = authorize(conn, project_id, actor_id, "read_licensed_pdf_and_public_questions")
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "document not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))

    return JSONResponse(
        status_code=200,
        content={
            "id": str(doc_id),
            "document_version_id": str(doc_id),
            "project_id": str(project_id),
            "resource_id": str(resource_id),
            "artifact_id": str(artifact_id),
            "logical_id": logical_id,
            "revision": revision,
            "sha256": sha256,
            "page_count": page_count,
            "created_at": created_at.isoformat() if created_at else None,
            "created_by": str(created_by) if created_by else None,
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
router.add_api_route("/api/v1/documents", upload_document_handler, methods=["POST"], name="upload_document_v1")
router.add_api_route("/documents", upload_document_handler, methods=["POST"], name="upload_document_compat")
router.add_api_route("/api/v1/projects/{project_id}/documents", upload_document_handler, methods=["POST"], name="upload_project_document")

router.add_api_route("/api/v1/documents/{document_id}/original", download_document_handler, methods=["GET"], name="download_document_original_v1")
router.add_api_route("/documents/{document_id}/original", download_document_handler, methods=["GET"], name="download_document_original_compat")
router.add_api_route("/api/v1/documents/{document_id}/download", download_document_handler, methods=["GET"], name="download_document_v1")
router.add_api_route("/documents/{document_id}/download", download_document_handler, methods=["GET"], name="download_document_compat")

router.add_api_route("/api/v1/documents/{document_id}", get_document_handler, methods=["GET"], name="get_document_v1")
router.add_api_route("/documents/{document_id}", get_document_handler, methods=["GET"], name="get_document_compat")

router.add_api_route("/api/v1/documents/{document_id}/pages/{page_index}", get_page_handler, methods=["GET"], name="get_document_page_v1")
router.add_api_route("/documents/{document_id}/pages/{page_index}", get_page_handler, methods=["GET"], name="get_document_page_compat")
