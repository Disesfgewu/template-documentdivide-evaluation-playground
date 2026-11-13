"""Run status (SPEC 8 ``GET /runs/{id}``).

Owner, maintainer and reviewer see any run of their project. A participant
sees only a run of a method revision they own, and any other run looks
absent (404). An observer only ever gets published aggregates, which do not
exist yet, so an observer gets 403. The response carries counts, never
answers, payloads or scores.
"""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
GOLD_READERS = ("owner", "maintainer", "reviewer")


def _get_run(conn: psycopg.Connection, actor_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    row = conn.execute(
        "SELECT r.project_id, r.status::text, r.budget_profile, r.finalized_at, r.benchmark_id, "
        "r.method_revision_id, m.owner_actor_id, rv.logical_id, rv.revision "
        "FROM runs r JOIN method_revisions m ON m.id = r.method_revision_id "
        "JOIN benchmark_revisions br ON br.id = r.benchmark_id "
        "JOIN resource_versions rv ON rv.id = br.resource_id WHERE r.id = %s", (run_id,)).fetchone()
    missing = ApiError(404, "not_found", "run not found")
    if row is None:
        raise missing
    (project_id, status, budget_profile, finalized_at, benchmark_id, method_revision_id, method_owner,
     bench_logical, bench_revision) = row
    try:
        decision = authorize(conn, project_id, actor_id, "read_run_results")
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if decision.role in GOLD_READERS:
        pass
    elif decision.role == "participant" and method_owner == actor_id:
        pass
    elif decision.role == "participant":
        raise missing
    else:  # observer: published aggregates only, none exist yet
        raise ApiError(403, "permission_denied", "observer may only read published aggregates")

    # READ COMMITTED assigns a snapshot per statement.  Read the run metadata
    # and all counters together after authorization, so a concurrent commit
    # cannot create a response assembled from different points in time.
    snapshot = conn.execute(
        "SELECT r.project_id, r.status::text, r.budget_profile, r.finalized_at, r.benchmark_id, "
        "r.method_revision_id, m.owner_actor_id, rv.logical_id, rv.revision, "
        "(SELECT count(*) FROM active_questions q WHERE q.benchmark_id = r.benchmark_id), "
        "(SELECT count(*) FROM attempts a WHERE a.run_id = r.id), "
        "(SELECT count(*) FROM submissions s WHERE s.run_id = r.id AND s.status = 'accepted' AND s.answer_status = 'answered'), "
        "(SELECT count(*) FROM submissions s WHERE s.run_id = r.id AND s.status = 'accepted' AND s.answer_status = 'abstained'), "
        "(SELECT count(*) FROM submissions s WHERE s.run_id = r.id AND s.status = 'rejected'), "
        "(SELECT count(*) FROM submissions s WHERE s.run_id = r.id AND s.status = 'late') "
        "FROM runs r JOIN method_revisions m ON m.id = r.method_revision_id "
        "JOIN benchmark_revisions br ON br.id = r.benchmark_id "
        "JOIN resource_versions rv ON rv.id = br.resource_id WHERE r.id = %s", (run_id,)).fetchone()
    if snapshot is None:
        raise missing
    (snapshot_project, status, budget_profile, finalized_at, benchmark_id, method_revision_id, snapshot_owner,
     bench_logical, bench_revision, total, attempts, answered, abstained, rejected, late) = snapshot
    # Recheck identities used for authorization if the run changed between
    # the authorization lookup and its snapshot. Do not expose its counts.
    if snapshot_project != project_id or (decision.role == "participant" and snapshot_owner != actor_id):
        raise missing
    return {
        "id": str(run_id), "project_id": str(project_id), "status": status,
        "benchmark": {"revision_id": str(benchmark_id), "logical_id": bench_logical, "revision": bench_revision},
        "method_revision_id": str(method_revision_id), "budget_profile": budget_profile,
        "finalized_at": finalized_at.isoformat() if finalized_at else None,
        "progress": {
            "question_count": int(total), "attempts": int(attempts),
            "accepted_answered": answered, "accepted_abstained": abstained,
            "pending": max(int(total) - answered - abstained, 0),
            "rejected_submissions": int(rejected),
            "late_submissions": int(late),
        },
    }


async def get_run_handler(run_id: uuid.UUID, request: Request,
                          conn: psycopg.Connection = Depends(get_db),
                          actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    content = await _run_blocking(request, "meta", _get_run, conn, actor_id, run_id)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/runs/{run_id}", get_run_handler, methods=["GET"], name="get_run_v1")
