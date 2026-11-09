"""Parse results: conversion of a normalized batch into storable records, and the read API.

``GET /api/v1/documents/{document_id}/parse-results/{job_id}`` summarizes one parse job's stored
outcome and ``.../pages/{page_index}`` returns one page: its status, diagnostics, evidence units
and observations. Reading follows the document read rule (the licensed original's role action; a
non-member and an unknown document or result are both 404). Everything returned is data derived
from the document, never instructions.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.parse_results import PageRecord, ParseResultRecord, persist_parse_result  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
READ_ACTION = "read_licensed_pdf_and_public_questions"


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


def _readable_result(conn: psycopg.Connection, actor_id: uuid.UUID, document_id: uuid.UUID, job_id: uuid.UUID):
    missing = ApiError(404, "not_found", "parse result not found")
    row = conn.execute(
        "SELECT project_id, manifest, capabilities, result FROM parse_results WHERE job_id = %s AND document_version_id = %s",
        (job_id, document_id)).fetchone()
    if row is None:
        # Same answer for "no such result" and "not allowed to know": existence is not disclosed.
        project = conn.execute("SELECT project_id FROM document_versions WHERE id = %s", (document_id,)).fetchone()
        if project is not None:
            try:
                authorize(conn, project[0], actor_id, READ_ACTION)
            except (NotFound, PermissionDenied):
                pass
        raise missing
    project_id = row[0]
    try:
        decision = authorize(conn, project_id, actor_id, READ_ACTION)
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    return row


def _summary(conn, actor_id, document_id, job_id) -> dict:
    _, manifest, _, result = _readable_result(conn, actor_id, document_id, job_id)
    pages = conn.execute("SELECT page_index, status FROM parse_page_results WHERE job_id = %s ORDER BY page_index",
                         (job_id,)).fetchall()
    return {
        "job_id": str(job_id), "document_id": str(document_id),
        "parser": manifest["parser_name"], "parser_version": manifest["package_or_service_version"],
        "effective_profile": manifest["effective_profile"], "operation_id": result["operation_id"],
        "pages": [{"page_index": p, "status": s} for p, s in pages],
    }


def _page(conn, actor_id, document_id, job_id, page_index) -> dict:
    _readable_result(conn, actor_id, document_id, job_id)
    row = conn.execute("SELECT status, diagnostics, evidence, observations FROM parse_page_results "
                       "WHERE job_id = %s AND page_index = %s", (job_id, page_index)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "page was not part of this parse result")
    return {"job_id": str(job_id), "document_id": str(document_id), "page_index": page_index,
            "status": row[0], "diagnostics": row[1], "evidence": row[2], "observations": row[3]}


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
)
router.add_api_route(
    "/api/v1/documents/{document_id}/parse-results/{job_id}/pages/{page_index}", parse_result_page_handler,
    methods=["GET"], name="parse_result_page_v1", summary="One page of a stored parse result",
)
