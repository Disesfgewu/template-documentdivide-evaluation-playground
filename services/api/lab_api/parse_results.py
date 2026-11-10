"""Parse results: conversion of a normalized batch into storable records, and the read API.

``GET /api/v1/documents/{document_id}/parse-results/{job_id}`` summarizes one parse job's stored
outcome and ``.../pages/{page_index}`` returns one page: its status, diagnostics, evidence units
and observations. Reading needs the role action ``read_gold_and_generation_context`` (owner,
maintainer, reviewer). The organizer's parse output is the source of the evidence the benchmark is
generated from, and SPEC 16.4 keeps participants' parsing separate from it, so a participant gets
403 and an observer gets 403 whether or not a result exists; a non-member, an unknown document,
an unknown result and a page outside the result are 404. Everything returned is data derived from
the document, never instructions.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from typing import Literal
import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.parse_results import PageRecord, ParseResultRecord, persist_parse_result  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
READ_ACTION = "read_gold_and_generation_context"


def record_from_batch(batch) -> ParseResultRecord:
    """Validated, JSON-safe record of a ``NormalizedBatch`` (never a quarantined one)."""
    if batch.quarantine_reason is not None:
        raise ValueError("a quarantined batch has no result to store")
    result = batch.result.model_dump(mode="json")
    contents = [a.content for a in batch.artifacts]
    refs = {"sha256:" + hashlib.sha256(c).hexdigest() for c in contents}
    missing = [r for r in result["raw_result_refs"] if r not in refs]
    if missing:
        raise ValueError("raw_result_refs point at bytes that are not in the batch")
    evidence = [e.model_dump(mode="json") for e in batch.evidence]
    observations = [{**asdict(o), "upstream_image_refs": list(o.upstream_image_refs)} for o in batch.observations]
    pages = []
    for page in result["pages"]:
        index = page["page_index"]
        pages.append(PageRecord(
            page_index=index, status=page["status"], diagnostics=page["diagnostics"],
            evidence=[e for e in evidence if e["regions"][0]["page_index"] == index],
            observations=[o for o in observations if o["page_index"] == index]))
    return ParseResultRecord(
        manifest=batch.manifest.model_dump(mode="json"), capabilities=batch.capabilities.model_dump(mode="json"),
        result=result, raw_artifacts=tuple(contents), pages=tuple(pages))


def make_persist(store):
    """The ``persist`` callable for ``parse_worker.process_parse_job``: converts the batch and
    stores it through ``persist_parse_result`` inside the job's finish transaction."""
    def persist(conn, project_id, lease, batch):
        persist_parse_result(conn, store, project_id, lease, record_from_batch(batch))
    return persist


def _authorize_read(conn: psycopg.Connection, actor_id: uuid.UUID, document_id: uuid.UUID) -> uuid.UUID:
    """Authorization depends on the document only, never on whether a result exists."""
    row = conn.execute("SELECT project_id FROM document_versions WHERE id = %s", (document_id,)).fetchone()
    missing = ApiError(404, "not_found", "parse result not found")
    if row is None:
        raise missing
    try:
        authorize(conn, row[0], actor_id, READ_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    return row[0]


def _summary(conn, actor_id, document_id, job_id) -> dict:
    _authorize_read(conn, actor_id, document_id)
    # Only the few fields the summary shows: never the whole result document.
    row = conn.execute(
        "SELECT operation_id, manifest->>'parser_name', manifest->>'package_or_service_version', "
        "manifest->>'effective_profile' FROM parse_results WHERE job_id = %s AND document_version_id = %s",
        (job_id, document_id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "parse result not found")
    pages = conn.execute("SELECT page_index, status FROM parse_page_results WHERE job_id = %s ORDER BY page_index",
                         (job_id,)).fetchall()
    profile = row[3]
    # A manifest always carries its effective profile; a row that lacks one must not turn a read into a 500.
    strategy = profile.rsplit("-", 1)[-1] if isinstance(profile, str) and profile else None
    return {
        "job_id": str(job_id), "document_id": str(document_id), "parser": row[1], "parser_version": row[2],
        "effective_profile": profile, "strategy": strategy, "operation_id": str(row[0]),
        "pages": [{"page_index": p, "status": s} for p, s in pages],
    }


def _page(conn, actor_id, document_id, job_id, page_index) -> dict:
    _authorize_read(conn, actor_id, document_id)
    row = conn.execute(
        "SELECT status, diagnostics, evidence, observations FROM parse_page_results "
        "WHERE job_id = %s AND document_version_id = %s AND page_index = %s",
        (job_id, document_id, page_index)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "page was not part of this parse result")
    return {"job_id": str(job_id), "document_id": str(document_id), "page_index": page_index,
            "status": row[0], "diagnostics": row[1], "evidence": row[2], "observations": row[3]}


class PageStatusModel(BaseModel):
    page_index: int
    status: Literal["ok", "partial", "needs_review", "failed"]


class ParseResultSummaryModel(BaseModel):
    job_id: str
    document_id: str
    parser: str
    parser_version: str
    effective_profile: str | None = None
    strategy: str | None = None
    operation_id: str
    pages: list[PageStatusModel]


class ParsePageModel(BaseModel):
    job_id: str
    document_id: str
    page_index: int
    status: Literal["ok", "partial", "needs_review", "failed"]
    diagnostics: list[str]
    evidence: list[dict]      # EvidenceUnit objects, geometry exactly as stored
    observations: list[dict]  # raw upstream observations with their source pointers


async def parse_result_summary_handler(
    document_id: uuid.UUID, job_id: uuid.UUID, request: Request,
    conn: psycopg.Connection = Depends(get_db), actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    body = await _run_blocking(request, "meta", _summary, conn, actor_id, document_id, job_id)
    return JSONResponse(status_code=200, content=body)


async def parse_result_page_handler(
    document_id: uuid.UUID, job_id: uuid.UUID, page_index: int, request: Request,
    conn: psycopg.Connection = Depends(get_db), actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    if page_index < 0:
        raise ApiError(400, "bad_request", "page_index must be a non-negative integer")
    body = await _run_blocking(request, "meta", _page, conn, actor_id, document_id, job_id, page_index)
    return JSONResponse(status_code=200, content=body)


router.add_api_route(
    "/api/v1/documents/{document_id}/parse-results/{job_id}", parse_result_summary_handler, methods=["GET"],
    name="parse_result_summary_v1", summary="Summary of a stored parse result",
    responses={200: {"model": ParseResultSummaryModel}},
)
router.add_api_route(
    "/api/v1/documents/{document_id}/parse-results/{job_id}/pages/{page_index}", parse_result_page_handler,
    methods=["GET"], name="parse_result_page_v1", summary="One page of a stored parse result",
    responses={200: {"model": ParsePageModel}},
)
