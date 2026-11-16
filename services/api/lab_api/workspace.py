"""What already exists in the project, so the owner can RESUME after a restart or upgrade (Issue #194).

``GET /api/v1/workspace`` lists the project's documents and benchmark revisions with the facts the web page needs to reopen the
workflow at the right step: the document, its latest parse job, the benchmark status, how many questions it has, how many
uploaded developer methods. Counts and states only: no gold, no question text, no answers (so any member who may read the
plan may call it). Bounded: the newest 200 of each."""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .app_log import app_traced
from .benchmark_build import READ_ACTION, BENCHMARK_RESOURCE_TYPE
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from lab_db.repo import NotFound, PermissionDenied, authorize

router = APIRouter()
LIMIT = 200


@app_traced(stage="workspace", is_stage=True)
def _workspace(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID) -> dict:
    try:
        authorize(conn, project_id, actor_id, READ_ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "project not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    with conn.transaction():
        docs = conn.execute(
            "SELECT dv.id, rv.logical_id, rv.revision, dv.page_count, a.size_bytes, dv.sha256, rv.created_at, "
            "(SELECT pj.job_id FROM parse_jobs pj JOIN jobs j ON j.project_id = pj.project_id AND j.id = pj.job_id "
            "  WHERE pj.project_id = dv.project_id AND pj.document_version_id = dv.id ORDER BY pj.created_at DESC, pj.job_id DESC LIMIT 1), "
            "(SELECT j.status::text FROM parse_jobs pj JOIN jobs j ON j.project_id = pj.project_id AND j.id = pj.job_id "
            "  WHERE pj.project_id = dv.project_id AND pj.document_version_id = dv.id ORDER BY pj.created_at DESC, pj.job_id DESC LIMIT 1), "
            "(SELECT count(*) FROM benchmark_revisions b WHERE b.document_version_id = dv.id) "
            "FROM document_versions dv JOIN resource_versions rv ON rv.id = dv.resource_id JOIN artifacts a ON a.id = dv.artifact_id "
            "WHERE dv.project_id = %s AND a.state::text = 'committed' ORDER BY rv.created_at DESC, dv.id DESC LIMIT %s",
            (project_id, LIMIT)).fetchall()
        benches = conn.execute(
            "SELECT rv.logical_id, rv.revision, b.status::text, b.document_version_id, drv.logical_id, rv.created_at, b.frozen_at, "
            "(SELECT count(*) FROM active_questions q WHERE q.benchmark_id = b.id), "
            "(SELECT count(*) FROM questions q WHERE q.benchmark_id = b.id), "
            "(SELECT count(*) FROM external_submissions e WHERE e.benchmark_id = b.id), "
            "(SELECT count(*) FROM runs r WHERE r.benchmark_id = b.id), "
            "(SELECT bps.parse_job_id FROM benchmark_parse_sources bps WHERE bps.benchmark_id = b.id) "
            "FROM benchmark_revisions b JOIN resource_versions rv ON rv.id = b.resource_id "
            "JOIN document_versions dv ON dv.id = b.document_version_id JOIN resource_versions drv ON drv.id = dv.resource_id "
            "WHERE b.project_id = %s AND rv.resource_type = %s ORDER BY rv.created_at DESC, b.id DESC LIMIT %s",
            (project_id, BENCHMARK_RESOURCE_TYPE, LIMIT)).fetchall()
    with conn.transaction():
        flags: dict = {}
        for did, code in conn.execute("SELECT document_version_id, code FROM document_version_flags WHERE project_id = %s ORDER BY code", (project_id,)):
            flags.setdefault(did, []).append(code)
    documents = [{"id": str(i), "document_version_id": str(i), "flags": flags.get(i, []), "project_id": str(project_id), "logical_id": lid, "revision": int(rev),
                  "page_count": int(pages), "size_bytes": int(size), "sha256": sha, "uploaded_at": created.isoformat(),
                  "latest_parse_job_id": str(job) if job else None, "latest_parse_status": pstatus, "benchmarks": int(nb)}
                 for i, lid, rev, pages, size, sha, created, job, pstatus, nb in docs]
    benchmarks = [{"logical_id": lid, "revision": int(rev), "status": status, "document_id": str(doc), "document_logical_id": dlid,
                   "created_at": created.isoformat(), "frozen_at": frozen.isoformat() if frozen else None,
                   "active_questions": int(active), "questions": int(total), "uploads": int(uploads), "runs": int(runs),
                   "parse_job_id": str(pjob) if pjob else None}
                  for lid, rev, status, doc, dlid, created, frozen, active, total, uploads, runs, pjob in benches]
    return {"project_id": str(project_id), "documents": documents, "benchmarks": benchmarks,
            "counts": {"documents": len(documents), "benchmarks": len(benchmarks)},
            "note": "counts and states only; the newest %d of each" % LIMIT}


async def workspace_handler(request: Request, conn: psycopg.Connection = Depends(get_db),
                            actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _workspace, conn, actor_id, project_id))


router.add_api_route("/api/v1/workspace", workspace_handler, methods=["GET"], name="workspace_v1")
