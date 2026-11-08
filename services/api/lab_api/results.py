"""Run results (SPEC 8 ``GET /runs/{id}/results``, SPEC 9.1).

Per-metric summaries and a page of per-question scores for one evaluation of
a run. Each metric is reported on its own, 0-100; no weighted total is ever
produced (SPEC 9.1). A summary states how many questions were scored, were
not applicable, are pending or errored, so an N/A or missing score is never
averaged in as zero. The mean is the plain mean of ``raw_value`` over scored
rows.

Access: owner, maintainer and reviewer only. A participant's view of scores
depends on a feedback profile the SPEC does not define yet, so a participant
who owns the run gets 403 (others' runs look absent, 404), and an observer
gets 403. PM is asked on Issue #12.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from .runs import GOLD_READERS
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

router = APIRouter()
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
METRICS = ("recall_at_k", "complete_at_k", "correctness", "textual_consistency", "localization_precision",
           "citation_precision", "citation_completeness", "faithfulness")
BUCKETS = ("general", "cross_page", "chart_table", "confusable")
KNOWN_PARAMS = {"evaluation_id", "metric_name", "quota_bucket", "limit", "cursor"}


def bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def parse_query(params) -> dict:
    unknown = set(params) - KNOWN_PARAMS
    if unknown:
        raise bad(f"unknown query parameter: {sorted(unknown)[0][:40]}")
    q: dict = {"limit": DEFAULT_LIMIT}
    if "evaluation_id" in params:
        try:
            q["evaluation_id"] = uuid.UUID(params["evaluation_id"])
        except ValueError:
            raise bad("evaluation_id must be a UUID") from None
    if "metric_name" in params:
        if params["metric_name"] not in METRICS:
            raise bad(f"metric_name must be one of {list(METRICS)}")
        q["metric_name"] = params["metric_name"]
    if "quota_bucket" in params:
        if params["quota_bucket"] not in BUCKETS:
            raise bad(f"quota_bucket must be one of {list(BUCKETS)}")
        q["quota_bucket"] = params["quota_bucket"]
    if "limit" in params:
        raw = params["limit"]
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= MAX_LIMIT:
            raise bad(f"limit must be an integer 1..{MAX_LIMIT}")
        q["limit"] = int(raw)
    q["cursor"] = params.get("cursor")
    return q


def encode_cursor(evaluation_id: uuid.UUID, ordinal: int, metric: str) -> str:
    raw = json.dumps({"e": str(evaluation_id), "o": ordinal, "m": metric}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, evaluation_id: uuid.UUID) -> tuple[int, str]:
    err = ApiError(400, "invalid_cursor", "invalid cursor")
    try:
        if len(cursor) > 300:
            raise err
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        raise err from None
    if (not isinstance(data, dict) or set(data) != {"e", "o", "m"} or data["e"] != str(evaluation_id)
            or not isinstance(data["o"], int) or isinstance(data["o"], bool) or data["o"] < 0
            or data["m"] not in METRICS):
        raise err
    return data["o"], data["m"]


def _authorize_results(conn: psycopg.Connection, actor_id: uuid.UUID, run_id: uuid.UUID) -> tuple:
    row = conn.execute("SELECT r.project_id, r.benchmark_id, m.owner_actor_id FROM runs r "
                       "JOIN method_revisions m ON m.id = r.method_revision_id WHERE r.id = %s", (run_id,)).fetchone()
    missing = ApiError(404, "not_found", "run not found")
    if row is None:
        raise missing
    project_id, benchmark_id, method_owner = row
    try:
        decision = authorize(conn, project_id, actor_id, "read_run_results")
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if decision.role in GOLD_READERS:
        return project_id, benchmark_id
    if decision.role == "participant" and method_owner != actor_id:
        raise missing
    raise ApiError(403, "permission_denied", "scores are not available to this role yet")


def _get_results(conn: psycopg.Connection, actor_id: uuid.UUID, run_id: uuid.UUID, q: dict) -> dict:
    project_id, benchmark_id = _authorize_results(conn, actor_id, run_id)
    if "evaluation_id" in q:
        ev = conn.execute("SELECT id, grader_revision, status, created_at FROM evaluations WHERE id = %s AND run_id = %s",
                          (q["evaluation_id"], run_id)).fetchone()
    else:
        ev = conn.execute("SELECT id, grader_revision, status, created_at FROM evaluations WHERE run_id = %s "
                          "ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()
    if ev is None:
        raise ApiError(404, "not_found", "evaluation not found")
    eval_id, grader, ev_status, created_at = ev
    after = decode_cursor(q["cursor"], eval_id) if q["cursor"] else None

    where, args = ["s.evaluation_id = %s"], [eval_id]
    for col, key in (("s.metric_name", "metric_name"), ("qu.quota_bucket", "quota_bucket")):
        if key in q:
            where.append(f"{col} = %s")
            args.append(q[key])
    base = ("FROM question_scores s JOIN questions qu ON qu.benchmark_id = s.benchmark_id AND qu.id = s.question_id "
            "WHERE " + " AND ".join(where))
    summary_rows = conn.execute(
        "SELECT s.metric_name, count(*) FILTER (WHERE s.status = 'scored'), "
        "count(*) FILTER (WHERE s.status = 'not_applicable'), count(*) FILTER (WHERE s.status = 'pending'), "
        "count(*) FILTER (WHERE s.status = 'error'), avg(s.raw_value) FILTER (WHERE s.status = 'scored') "
        + base + " GROUP BY s.metric_name ORDER BY s.metric_name", tuple(args)).fetchall()
    page_where, page_args = base, list(args)
    if after:
        page_where += " AND (qu.ordinal, s.metric_name) > (%s, %s)"
        page_args += [after[0], after[1]]
    rows = conn.execute(
        "SELECT qu.id, qu.ordinal, qu.quota_bucket, s.metric_name, s.status, s.raw_value, s.numerator, "
        "s.denominator, s.reason_codes, s.submission_id " + page_where +
        " ORDER BY qu.ordinal, s.metric_name LIMIT %s", (*page_args, q["limit"] + 1)).fetchall()
    page = rows[:q["limit"]]
    return {
        "run_id": str(run_id),
        "evaluation": {"id": str(eval_id), "grader_revision": grader, "status": ev_status,
                       "created_at": created_at.isoformat()},
        "filters": {k: q[k] for k in ("metric_name", "quota_bucket") if k in q},
        "metrics": [{"metric_name": m, "scored": int(sc), "not_applicable": int(na), "pending": int(pe),
                     "error": int(er), "mean": float(mean) if mean is not None else None}
                    for m, sc, na, pe, er, mean in summary_rows],
        "items": [{"question_id": str(r[0]), "ordinal": r[1], "quota_bucket": r[2], "metric_name": r[3],
                   "status": r[4], "raw_value": r[5], "numerator": r[6], "denominator": r[7],
                   "reason_codes": list(r[8]), "submission_id": str(r[9]) if r[9] else None} for r in page],
        "next_cursor": encode_cursor(eval_id, page[-1][1], page[-1][3]) if len(rows) > q["limit"] else None,
    }


async def get_results_handler(run_id: uuid.UUID, request: Request,
                              conn: psycopg.Connection = Depends(get_db),
                              actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    q = parse_query(request.query_params)
    content = await _run_blocking(request, "meta", _get_results, conn, actor_id, run_id, q)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/runs/{run_id}/results", get_results_handler, methods=["GET"], name="get_run_results_v1")
