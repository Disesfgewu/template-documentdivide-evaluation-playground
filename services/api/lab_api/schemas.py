"""Typed response, request and error schemas for the LAB API (SPEC 8, 16.4)."""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(ResponseModel):
    """Observed HTTP envelope, distinct from the stricter versioned contract.

    In particular, the current handler echoes X-Request-Id without the
    ResourceId length checks. Metadata must not claim those checks run.
    """
    code: str
    message: str
    retryable: bool
    request_id: str
    details: dict[str, Any]
    retry_after: None


class ErrorResponse(ResponseModel):
    error: ErrorBody


class HealthzResponse(ResponseModel):
    status: Literal["ok"] = Field(..., description="Liveness status indicator")


class ReadyzCheckResult(ResponseModel):
    ok: bool = Field(..., description="Whether the dependency check passed")
    detail: Optional[Any] = Field(None, description="Check details or failure message")


class ReadyzResponse(ResponseModel):
    status: str = Field(..., description="Overall readiness: ready or not_ready")
    env: str = Field(..., description="Configured environment (e.g. dev, test, staging, prod)")
    provider_mode: str = Field(..., description="Configured provider mode (mock or real)")
    checks: dict[str, ReadyzCheckResult] = Field(..., description="Results of individual dependency checks")


class DocumentUploadForm(BaseModel):
    # Arbitrary PDF-typed field names are accepted by the strict multipart
    # parser. Part headers/exactly-one-PDF are enforced at runtime and are
    # described on requestBody; JSON Schema alone cannot express them.
    model_config = ConfigDict(extra="allow", json_schema_extra={"minProperties": 1, "maxProperties": 2})
    __pydantic_extra__: dict[str, bytes] = Field(init=False)
    file: Optional[bytes] = Field(None, description="Conventional PDF binary field; pdf/document aliases also accepted")
    logical_id: Optional[str] = Field(
        None, description="Raw text part: at most 512 UTF-8 bytes, stripped before selecting an effective 1..128-character logical ID; empty falls back to query/header/default"
    )


class DocumentVersionResponse(ResponseModel):
    id: uuid.UUID = Field(..., description="Document version identifier")
    document_version_id: uuid.UUID = Field(..., description="Document version identifier alias")
    project_id: uuid.UUID = Field(..., description="Project identifier")
    resource_id: uuid.UUID = Field(..., description="Resource version identifier")
    artifact_id: uuid.UUID = Field(..., description="Stored artifact identifier")
    logical_id: str = Field(..., min_length=1, max_length=128, description="Logical document identifier")
    revision: int = Field(..., ge=1, description="Resource revision number")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="SHA-256 hex digest of the PDF file")
    size_bytes: int = Field(..., ge=0, description="Size of the document in bytes")
    page_count: int = Field(..., ge=1, description="Total number of pages in the document")
    flagged: bool = Field(..., description="Whether the document was flagged during inspection")
    warnings: list[str] = Field(..., description="Warnings identified during inspection")


class DocumentUploadReplayResponse(ResponseModel):
    id: uuid.UUID = Field(..., description="Document version identifier")
    document_version_id: uuid.UUID = Field(..., description="Document version identifier alias")
    project_id: uuid.UUID = Field(..., description="Project identifier")
    resource_id: uuid.UUID = Field(..., description="Resource version identifier")
    artifact_id: uuid.UUID = Field(..., description="Stored artifact identifier")
    logical_id: str = Field(..., min_length=1, max_length=128, description="Logical document identifier")
    revision: int = Field(..., ge=1, description="Resource revision number")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="SHA-256 hex digest of the PDF file")
    size_bytes: int = Field(..., ge=0, description="Size of the document in bytes")
    page_count: int = Field(..., ge=1, description="Total number of pages in the document")
    replayed: Literal[True] = Field(..., description="Indicates an idempotent replay of a previously processed upload")
    flagged: Optional[bool] = Field(None, description="Whether the document was flagged during inspection")
    warnings: Optional[list[str]] = Field(None, description="Warnings identified during inspection")


class DocumentMetadataResponse(ResponseModel):
    id: uuid.UUID = Field(..., description="Document version identifier")
    document_version_id: uuid.UUID = Field(..., description="Document version identifier alias")
    project_id: uuid.UUID = Field(..., description="Project identifier")
    resource_id: uuid.UUID = Field(..., description="Resource version identifier")
    artifact_id: uuid.UUID = Field(..., description="Stored artifact identifier")
    logical_id: str = Field(..., min_length=1, max_length=128, description="Logical document identifier")
    revision: int = Field(..., ge=1, description="Resource revision number")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="SHA-256 hex digest of the PDF file")
    size_bytes: int = Field(..., ge=0, description="Size of the document in bytes")
    page_count: int = Field(..., ge=1, description="Total number of pages in the document")


class PageGeometryResponse(ResponseModel):
    document_version_id: uuid.UUID = Field(..., description="Document version identifier")
    page_index: int = Field(..., ge=0, description="0-based page index")
    page_label: Optional[str] = Field(..., description="Page label if specified in PDF catalog")
    rotation: Literal[0, 90, 180, 270] = Field(..., description="Clockwise rotation in degrees")
    width_pt: float = Field(..., gt=0, description="Normalized display width in points (1/72 inch)")
    height_pt: float = Field(..., gt=0, description="Normalized display height in points (1/72 inch)")


class PublicQuestionItem(ResponseModel):
    id: uuid.UUID = Field(..., description="Question row identifier (not the internal question key)")
    question_text: str = Field(..., min_length=1, description="Text of the question without gold facts or answers")
    language: str = Field(..., pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$", description="Language tag of the question (e.g. en, zh-Hant)")


class PublicBenchmarkQuestionsResponse(ResponseModel):
    benchmark_revision_id: uuid.UUID = Field(..., description="Benchmark revision identifier")
    status: Literal["draft", "parsing", "planning", "generating", "validating", "ready_to_freeze", "frozen", "needs_review", "paused_budget", "failed", "cancelled"] = Field(..., description="Stored benchmark revision status; participant sees only frozen")
    items: list[PublicQuestionItem] = Field(..., description="List of public questions for this page")
    next_cursor: Optional[str] = Field(..., description="Opaque pagination cursor for subsequent page; null if final page")
