"""Run the local lexical baseline on a frozen benchmark (PM-DECISION item 3, Refs #10/#12).

``POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/baseline-run`` (owner or maintainer): runs the existing local
lexical method on every question of the frozen benchmark, records the run/attempts/submissions/hits in the existing tables
and scores it with the rule-based scorer (``lab_api.baseline_eval``). The scores are read with the existing
``GET /api/v1/runs/{run_id}/results``; this endpoint returns pointers and counts only, never scores or gold. No model is called."""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, get_store, resolve_project_id
from .benchmark_build import _find
from .baseline_eval import BaselineError, run_local_baseline
from lab_db.repo import NotFound, PermissionDenied
from lab_db.storage import LocalArtifactStore

router = APIRouter()


def _run(conn: psycopg.Connection, store: LocalArtifactStore, actor_id: uuid.UUID, project_id: uuid.UUID,
         logical_id: str, revision: int) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    try:
        benchmark_id, _, status, _ = _find(conn, project_id, logical_id, revision)
        result = run_local_baseline(conn, store, project_id, benchmark_id, actor_id)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except BaselineError as exc:
        raise ApiError(409, "conflict", f"the baseline cannot run: {exc}") from None
    result["results_url"] = f"/api/v1/runs/{result['run_id']}/results?evaluation_id={result['evaluation_id']}"
    result["limits"] = ["The method is a local lexical page-retrieval baseline (no model, no answer synthesis).",
                        "Only source/retrieval metrics are scored; correctness, citation and faithfulness are pending (semantic judge not run).",
                        "Compliance of the benchmark is partial; the evaluation is provisional."]
    return result


async def baseline_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                           store: LocalArtifactStore = Depends(get_store),
                           actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=201, content=await _run_blocking(
        request, "meta", _run, conn, store, actor_id, project_id, logical_id, revision))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/baseline-run", baseline_handler, methods=["POST"],
                     name="run_local_baseline_v1", status_code=201)
