"""OpenAPI 3.1 specification builder for the LAB API (SPEC 8, 16.4, R22, T31).

Generates truthful contract metadata for currently implemented endpoints:
- Document upload (raw application/pdf and strict multipart/form-data)
- Document metadata retrieval and content-negotiated download
- Document page normalized geometry (JSON only)
- Authorized binary PDF original download
- Benchmark public questions (gold-free logical ID and revision)
- Liveness (/healthz) and readiness (/readyz) probes

Reflects configured runtime limits, authentication mechanisms, authorization
roles, idempotency semantics, and error envelopes without altering runtime
business validation or HTTP behavior.
"""
from __future__ import annotations

from typing import Any
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic.json_schema import models_json_schema

from .documents import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_UPLOAD_CONCURRENCY,
    DEFAULT_UPLOAD_READ_TIMEOUT_S,
)
from .benchmarks import DEFAULT_LIMIT, MAX_LIMIT
from .schemas import (
    DocumentMetadataResponse,
    DocumentUploadForm,
    DocumentUploadReplayResponse,
    DocumentVersionResponse,
    ErrorBody,
    ErrorResponse,
    HealthzResponse,
    PageGeometryResponse,
    PublicBenchmarkQuestionsResponse,
    PublicQuestionItem,
    ReadyzCheckResult,
    ReadyzResponse,
)

CONTRACT_MODELS = [
    ErrorBody,
    ErrorResponse,
    HealthzResponse,
    ReadyzCheckResult,
    ReadyzResponse,
    DocumentUploadForm,
    DocumentVersionResponse,
    DocumentUploadReplayResponse,
    DocumentMetadataResponse,
    PageGeometryResponse,
    PublicQuestionItem,
    PublicBenchmarkQuestionsResponse,
]

REF_TEMPLATE = "#/components/schemas/{model}"


def _ref(model_name: str) -> dict[str, str]:
    return {"$ref": REF_TEMPLATE.format(model=model_name)}


def _json_content(model_name: str, description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _ref(model_name)}}}


def _error_response(description: str) -> dict[str, Any]:
    return _json_content("ErrorResponse", description)


def _validation_error_response(include_contract_error: bool = True) -> dict[str, Any]:
    if include_contract_error:
        return {
            "description": "Validation error: rejected document/business semantics or request parameter validation",
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            _ref("ErrorResponse"),
                            {"$ref": "#/components/schemas/HTTPValidationError"},
                        ]
                    }
                }
            },
        }
    return {
        "description": "Request parameter validation error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
            }
        },
    }


def build_lab_openapi(app: FastAPI) -> dict[str, Any]:
    """Describe implemented routes, retaining metadata for other registered operations."""
    max_pdf_bytes = getattr(app.state, "max_pdf_bytes", DEFAULT_MAX_PDF_BYTES)
    max_pdf_pages = getattr(app.state, "max_pdf_pages", DEFAULT_MAX_PDF_PAGES)
    upload_concurrency = getattr(app.state, "upload_concurrency", DEFAULT_UPLOAD_CONCURRENCY)
    download_concurrency = getattr(app.state, "download_concurrency", DEFAULT_DOWNLOAD_CONCURRENCY)
    upload_timeout_s = getattr(app.state, "upload_read_timeout_s", DEFAULT_UPLOAD_READ_TIMEOUT_S)

    openapi_doc = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )

    openapi_doc["info"]["description"] = (
        "document-seperater-playground LAB API (SPEC 8, 16.4).\n\n"
        f"Configured runtime limits: max_pdf_bytes={max_pdf_bytes} bytes, "
        f"max_pdf_pages={max_pdf_pages}, upload_concurrency_slots={upload_concurrency}, "
        f"download_concurrency_slots={download_concurrency}, upload_timeout={upload_timeout_s}s, "
        f"benchmark_question_limit={DEFAULT_LIMIT} (max={MAX_LIMIT})."
    )

    components = openapi_doc.setdefault("components", {})
    components.setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Cryptographically signed actor Bearer token (Authorization: Bearer <token>). "
            "Caller-asserted identity headers (e.g. X-Actor-Id) are strictly rejected."
        ),
    }

    # Register all contract model schemas
    _, defs = models_json_schema([(m, "validation") for m in CONTRACT_MODELS], ref_template=REF_TEMPLATE)
    schemas = components.setdefault("schemas", {})
    for name, schema_def in defs.get("$defs", {}).items():
        schemas[name] = schema_def

    bearer_security = [{"BearerAuth": []}]

    # Reusable parameter definitions
    p_x_request_id = {
        "name": "X-Request-Id",
        "in": "header",
        "required": False,
        "description": "Optional correlation request identifier returned in error envelopes.",
        "schema": {"type": "string"},
    }
    p_query_project_id = {
        "name": "project_id",
        "in": "query",
        "required": False,
        "description": "Project identifier (UUID). Required if X-Project-Id header is not supplied.",
        "schema": {"type": "string", "format": "uuid"},
    }
    p_header_project_id = {
        "name": "X-Project-Id",
        "in": "header",
        "required": False,
        "description": "Project identifier (UUID). Required if project_id query parameter is not supplied.",
        "schema": {"type": "string", "format": "uuid"},
    }
    p_path_project_id = {
        "name": "project_id",
        "in": "path",
        "required": True,
        "description": "Project identifier (UUID).",
        "schema": {"type": "string", "format": "uuid"},
    }
    p_query_logical_id = {
        "name": "logical_id",
        "in": "query",
        "required": False,
        "description": "Logical document identifier (1..128 characters, defaults to 'default').",
        "schema": {"type": "string", "minLength": 1, "maxLength": 128, "default": "default"},
    }
    p_header_logical_id = {
        "name": "X-Logical-Id",
        "in": "header",
        "required": False,
        "description": "Optional logical document identifier (1..128 characters).",
        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
    }
    p_header_idempotency_key = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": False,
        "description": "Optional idempotency key (1..256 characters) for safe upload retries.",
        "schema": {"type": "string", "minLength": 1, "maxLength": 256},
    }
    p_path_document_id = {
        "name": "document_id",
        "in": "path",
        "required": True,
        "description": "Document version identifier (UUID).",
        "schema": {"type": "string", "format": "uuid"},
    }

    # Upload operation spec
    def make_upload_operation(path_has_project: bool, op_id: str, summary: str) -> dict[str, Any]:
        params = [p_path_project_id] if path_has_project else [p_query_project_id, p_header_project_id]
        params.extend([p_query_logical_id, p_header_logical_id, p_header_idempotency_key, p_x_request_id])
        return {
            "summary": summary,
            "description": (
                "Authenticated and authorized PDF intake endpoint (SPEC 8, 10.1, 12.3, 16.4). "
                "Requires Owner or Maintainer role on the project. Accepts raw application/pdf binary bytes "
                "or strict multipart/form-data with exactly one PDF part and optional plain-text logical_id. Conventional PDF names file/pdf/document are accepted; any other name requires an explicit application/pdf part type. Part-header and exactly-one-PDF restrictions are enforced by the multipart parser, beyond JSON Schema. Project selection is path, then project_id query, then X-Project-Id. Logical ID selection is form, query, then X-Logical-Id, defaulting to default. "
                "Bounded by admission slots and maximum size limits. "
                "Supports Idempotency-Key: identical successful requests return 200 with replayed=true; original rejections retain their status/error; "
                "conflicting payloads return 409."
            ),
            "operationId": op_id,
            "security": bearer_security,
            "parameters": params,
            "requestBody": {
                "required": True,
                "description": f"PDF document up to {max_pdf_bytes} bytes via raw application/pdf or multipart/form-data.",
                "content": {
                    "application/pdf": {
                        "schema": {"type": "string", "format": "binary", "description": "Raw PDF binary payload"}
                    },
                    "multipart/form-data": {
                        "schema": _ref("DocumentUploadForm")
                    },
                },
            },
            "responses": {
                "201": _json_content("DocumentVersionResponse", "Document successfully uploaded, inspected, and persisted"),
                "200": _json_content("DocumentUploadReplayResponse", "Idempotent replay of previously completed upload"),
                "400": _error_response("Bad request: missing/invalid project_id, invalid idempotency key, or malformed multipart body"),
                "401": _error_response("Unauthorized: missing/invalid Bearer token or prohibited identity spoofing headers"),
                "403": _error_response("Permission denied: actor lacks import_and_configure_parse permission on project"),
                "404": _error_response("Project not found"),
                "408": _error_response("Request timeout: body not received within deadline"),
                "409": _error_response("Conflict: idempotency key reused with different payload, or operation in progress"),
                "413": _error_response("Payload too large: PDF exceeds max_pdf_bytes limit"),
                "415": _error_response("Unsupported media type: Content-Type must be application/pdf or multipart/form-data"),
                "422": _validation_error_response(include_contract_error=True),
                "500": _error_response("Handled internal error, including authentication not configured"),
                "503": _error_response("Service unavailable: database/storage unavailable or server busy (upload admission saturated)"),
            },
        }

    # Download original operation spec
    def make_download_operation(op_id: str, summary: str) -> dict[str, Any]:
        return {
            "summary": summary,
            "description": (
                "Authorized project-scoped original PDF download (SPEC 8, 10.2). "
                "Allowed roles: Owner, Maintainer, Reviewer, Participant within the document project. Observer is denied; explicit grants are not implemented. "
                "Returns buffered, verified committed original PDF bytes."
            ),
            "operationId": op_id,
            "security": bearer_security,
            "parameters": [p_path_document_id, p_x_request_id],
            "responses": {
                "200": {
                    "description": "Original raw PDF file stream",
                    "content": {
                        "application/pdf": {
                            "schema": {"type": "string", "format": "binary"}
                        }
                    },
                    "headers": {
                        "Content-Disposition": {
                            "description": "Attachment disposition with filename",
                            "schema": {"type": "string"}
                        },
                        "Content-Length": {
                            "description": "Exact byte length of PDF stream",
                            "schema": {"type": "integer"}
                        },
                    },
                },
                "401": _error_response("Unauthorized: missing or invalid Bearer credentials"),
                "403": _error_response("Permission denied: insufficient project role or observer lacking explicit grant"),
                "404": _error_response("Document version not found or artifact not committed"),
                "422": _validation_error_response(include_contract_error=False),
                "500": _error_response("Storage corruption or internal error verifying artifact integrity"),
                "503": _error_response("Service unavailable: database unavailable or server busy (download admission saturated)"),
            },
        }

    # Metadata & content negotiation operation spec
    def make_metadata_operation(op_id: str, summary: str) -> dict[str, Any]:
        return {
            "summary": summary,
            "description": (
                "Get document version metadata, or download raw PDF if Accept header contains application/pdf (SPEC 8). "
                "Allowed roles: Owner, Maintainer, Reviewer, Participant within the document project. Observer is denied; explicit grants are not implemented."
            ),
            "operationId": op_id,
            "security": bearer_security,
            "parameters": [
                p_path_document_id,
                {
                    "name": "Accept",
                    "in": "header",
                    "required": False,
                    "description": "Accept header. If containing application/pdf, returns the raw PDF bytes; otherwise returns JSON metadata.",
                    "schema": {"type": "string", "default": "application/json"},
                },
                p_x_request_id,
            ],
            "responses": {
                "200": {
                    "description": "Document version metadata JSON, or raw PDF if Accept contains application/pdf",
                    "content": {
                        "application/json": {
                            "schema": _ref("DocumentMetadataResponse")
                        },
                        "application/pdf": {
                            "schema": {"type": "string", "format": "binary"}
                        },
                    },
                },
                "401": _error_response("Unauthorized: missing or invalid Bearer credentials"),
                "403": _error_response("Permission denied: insufficient project role or observer lacking explicit grant"),
                "404": _error_response("Document version not found or artifact not committed"),
                "422": _validation_error_response(include_contract_error=False),
                "500": _error_response("Storage corruption or internal error"),
                "503": _error_response("Service unavailable: database unavailable or server busy"),
            },
        }

    # Page geometry operation spec
    def make_page_geometry_operation(op_id: str, summary: str) -> dict[str, Any]:
        return {
            "summary": summary,
            "description": (
                "Get per-page normalized geometry and dimensions (SPEC 8). "
                "Current implementation serves geometry JSON ONLY (display dimensions, rotation, label). Owner/maintainer/reviewer/participant may read within the document project; observer is denied."
            ),
            "operationId": op_id,
            "security": bearer_security,
            "parameters": [
                p_path_document_id,
                {
                    "name": "page_index",
                    "in": "path",
                    "required": True,
                    "description": "0-based page index (must be 0 <= page_index < page_count).",
                    "schema": {"type": "integer", "minimum": 0},
                },
                p_x_request_id,
            ],
            "responses": {
                "200": _json_content("PageGeometryResponse", "Per-page normalized geometry and dimensions JSON"),
                "401": _error_response("Unauthorized: missing or invalid Bearer credentials"),
                "403": _error_response("Permission denied: insufficient project role or observer lacking explicit grant"),
                "404": _error_response("Document not found or page_index out of range"),
                "422": _validation_error_response(include_contract_error=False),
                "500": _error_response("Handled internal error, including authentication not configured"),
                "503": _error_response("Service unavailable: database unavailable"),
            },
        }

    # Benchmark public questions operation spec
    benchmarks_questions_op = {
        "summary": "List public benchmark questions",
        "description": (
            "Public question set of a benchmark revision (SPEC 8, 16.4). "
            "Selects a project-scoped opaque benchmark logical_id and exact revision; returns public question rows. "
            "Answers, facts, evidence sets and distractors are strictly omitted. "
            "Participants only see frozen benchmark revisions (drafts return 404). "
            "Supports cursor-based pagination bounded by limit (1..200, default 50)."
        ),
        "operationId": "listPublicQuestionsV1",
        "security": bearer_security,
        "parameters": [
            {
                "name": "logical_id",
                "in": "path",
                "required": True,
                "description": "Logical identifier of the benchmark resource (1..128 characters).",
                "schema": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            {
                "name": "revision",
                "in": "path",
                "required": True,
                "description": "Benchmark revision number (integer >= 1).",
                "schema": {"type": "integer", "minimum": 1, "maximum": 2147483647},
            },
            p_query_project_id,
            p_header_project_id,
            {
                "name": "cursor",
                "in": "query",
                "required": False,
                "description": "Opaque pagination cursor from previous page next_cursor.",
                "schema": {"type": "string", "maxLength": 200},
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "description": f"Maximum questions per page (integer 1..{MAX_LIMIT}, default {DEFAULT_LIMIT}).",
                "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
            p_x_request_id,
        ],
        "responses": {
            "200": _json_content("PublicBenchmarkQuestionsResponse", "Page of public questions with pagination cursor"),
            "400": _error_response("Bad request: missing project_id, invalid limit format/bounds, or malformed/mismatched cursor"),
            "401": _error_response("Unauthorized: missing or invalid Bearer credentials"),
            "403": _error_response("Permission denied: insufficient project role or observer lacking explicit grant"),
            "404": _error_response("Benchmark revision not found, or unfrozen revision requested by participant"),
            "422": _validation_error_response(include_contract_error=False),
            "500": _error_response("Invalid stored question payload"),
            "503": _error_response("Service unavailable: database unavailable"),
        },
    }

    # Healthz & Readyz operation specs
    healthz_op = {
        "summary": "Liveness probe",
        "description": "Liveness check indicating process is running and able to handle HTTP requests.",
        "operationId": "healthz",
        "security": [],
        "parameters": [],
        "responses": {
            "200": _json_content("HealthzResponse", "Process is alive"),
        },
    }

    readyz_op = {
        "summary": "Readiness probe",
        "description": "Readiness check verifying backend dependencies (PostgreSQL, storage root, etc.).",
        "operationId": "readyz",
        "security": [],
        "parameters": [],
        "responses": {
            "200": _json_content("ReadyzResponse", "Service dependencies are healthy and ready to serve"),
            "503": _json_content("ReadyzResponse", "Service is not ready (one or more dependency checks failed)"),
        },
    }

    paths = openapi_doc.setdefault("paths", {})
    contract_paths = {}

    # Map paths to their updated operations
    contract_paths["/healthz"] = {"get": healthz_op}
    contract_paths["/readyz"] = {"get": readyz_op}

    contract_paths["/api/v1/documents"] = {"post": make_upload_operation(False, "uploadDocumentV1", "Upload document")}
    contract_paths["/documents"] = {"post": make_upload_operation(False, "uploadDocumentCompat", "Upload document (compat)")}
    contract_paths["/api/v1/projects/{project_id}/documents"] = {
        "post": make_upload_operation(True, "uploadProjectDocument", "Upload document to project")
    }

    contract_paths["/api/v1/documents/{document_id}/original"] = {
        "get": make_download_operation("downloadDocumentOriginalV1", "Download document original PDF")
    }
    contract_paths["/documents/{document_id}/original"] = {
        "get": make_download_operation("downloadDocumentOriginalCompat", "Download document original PDF (compat)")
    }
    contract_paths["/api/v1/documents/{document_id}/download"] = {
        "get": make_download_operation("downloadDocumentV1", "Download document PDF")
    }
    contract_paths["/documents/{document_id}/download"] = {
        "get": make_download_operation("downloadDocumentCompat", "Download document PDF (compat)")
    }

    contract_paths["/api/v1/documents/{document_id}"] = {
        "get": make_metadata_operation("getDocumentV1", "Get document metadata or PDF")
    }
    contract_paths["/documents/{document_id}"] = {
        "get": make_metadata_operation("getDocumentCompat", "Get document metadata or PDF (compat)")
    }

    contract_paths["/api/v1/documents/{document_id}/pages/{page_index}"] = {
        "get": make_page_geometry_operation("getDocumentPageV1", "Get document page geometry")
    }
    contract_paths["/documents/{document_id}/pages/{page_index}"] = {
        "get": make_page_geometry_operation("getDocumentPageCompat", "Get document page geometry (compat)")
    }

    contract_paths["/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions"] = {
        "get": benchmarks_questions_op
    }

    # Overlay individual registered operations, preserving other methods and
    # never advertising a route absent from this application.
    for path, operations in contract_paths.items():
        for method, operation in operations.items():
            if method in paths.get(path, {}):
                paths[path][method] = operation

    return openapi_doc


def configure_openapi(app: FastAPI) -> None:
    """Attach the truthful OpenAPI document generator to the FastAPI application."""
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = build_lab_openapi(app)
        return app.openapi_schema

    app.openapi = custom_openapi
