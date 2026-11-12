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
        try:
            if not raw.isascii() or not raw.isdigit() or len(raw) > 3 or not 1 <= int(raw) <= MAX_LIMIT:
                raise bad(f"limit must be an integer 1..{MAX_LIMIT}")
            q["limit"] = int(raw)
        except ValueError:
            raise bad(f"limit must be an integer 1..{MAX_LIMIT}") from None
    if "cursor" in params:
        raw_cursor = params["cursor"]
        if not raw_cursor:
            raise ApiError(400, "invalid_cursor", "cursor must not be empty")
        q["cursor"] = raw_cursor
    else:
        q["cursor"] = None
    return q


def normalize_filters(q: dict) -> dict:
    return {k: q[k] for k in sorted(("metric_name", "quota_bucket")) if q and k in q and q[k] is not None}


def encode_cursor(evaluation_id: uuid.UUID, ordinal: int, metric: str, filters: dict | None = None) -> str:
    norm_filters = {k: filters[k] for k in sorted(("metric_name", "quota_bucket")) if filters and k in filters and filters[k] is not None}
    payload = {"e": str(evaluation_id), "f": norm_filters, "m": metric, "o": ordinal}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, evaluation_id: uuid.UUID, filters: dict | None = None) -> tuple[int, str]:
    err = ApiError(400, "invalid_cursor", "invalid cursor")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise err
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor.replace("-", "+").replace("_", "/") + pad, validate=True)
        data = json.loads(raw)
    except (ValueError, binascii.Error):
        raise err from None
    if not isinstance(data, dict) or set(data.keys()) != {"e", "f", "m", "o"}:
        raise err
    if data["e"] != str(evaluation_id):
        raise err
    if not isinstance(data["o"], int) or isinstance(data["o"], bool) or data["o"] < 0 or data["o"] > 2147483647:
        raise err
    if data["m"] not in METRICS:
        raise err
    if not isinstance(data["f"], dict) or not set(data["f"].keys()).issubset({"metric_name", "quota_bucket"}):
        raise err
    if "metric_name" in data["f"] and data["f"]["metric_name"] not in METRICS:
        raise err
    if "quota_bucket" in data["f"] and data["f"]["quota_bucket"] not in BUCKETS:
        raise err
    expected_f = {k: filters[k] for k in sorted(("metric_name", "quota_bucket")) if filters and k in filters and filters[k] is not None}
    if data["f"] != expected_f:
        raise err
    if encode_cursor(evaluation_id, data["o"], data["m"], expected_f) != cursor:
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
    missing = ApiError(404, "not_found", "run not found")
    query_filters = normalize_filters(q)
    after = None
    target_eval_id = q.get("evaluation_id")

    if q.get("cursor") is not None:
        if not isinstance(q["cursor"], str) or not q["cursor"] or len(q["cursor"]) > 512:
            raise ApiError(400, "invalid_cursor", "invalid cursor")
        try:
            pad = "=" * (-len(q["cursor"]) % 4)
            raw = base64.b64decode(q["cursor"].replace("-", "+").replace("_", "/") + pad, validate=True)
            peek_data = json.loads(raw)
            cursor_eval_id = uuid.UUID(peek_data["e"])
        except Exception:
            raise ApiError(400, "invalid_cursor", "invalid cursor") from None

        if target_eval_id is not None and target_eval_id != cursor_eval_id:
            raise ApiError(400, "invalid_cursor", "cursor evaluation mismatch")

        after = decode_cursor(q["cursor"], cursor_eval_id, query_filters)
        target_eval_id = cursor_eval_id

    eval_where = "WHERE run_id = r.id AND id = %s" if target_eval_id else "WHERE run_id = r.id ORDER BY created_at DESC, id DESC LIMIT 1"
    eval_args = [target_eval_id] if target_eval_id else []

    score_where = ["s.evaluation_id = te.eval_id"]
    score_args = []
    if "metric_name" in q:
        score_where.append("s.metric_name = %s")
        score_args.append(q["metric_name"])
    if "quota_bucket" in q:
        score_where.append("qu.quota_bucket = %s")
        score_args.append(q["quota_bucket"])
    score_where_sql = " AND ".join(score_where)

    page_where = []
    page_args = []
    if after:
        page_where.append("(s.ordinal, s.metric_name) > (%s, %s)")
        page_args.extend([after[0], after[1]])
    page_where_sql = f"WHERE {' AND '.join(page_where)}" if page_where else ""
    page_args.append(q["limit"] + 1)

    sql = f"""
WITH target_eval AS (
    SELECT e.id AS eval_id, e.grader_revision, e.status AS eval_status, e.created_at,
           r.project_id, m.owner_actor_id
    FROM runs r
    JOIN method_revisions m ON m.id = r.method_revision_id
    LEFT JOIN LATERAL (
        SELECT id, grader_revision, status, created_at
        FROM evaluations
        {eval_where}
    ) e ON true
    WHERE r.id = %s
),
filtered_scores AS (
    SELECT qu.id AS question_id, qu.ordinal, qu.quota_bucket, s.metric_name, s.status,
           s.raw_value, s.numerator, s.denominator, s.reason_codes, s.submission_id
    FROM target_eval te
    JOIN question_scores s ON s.evaluation_id = te.eval_id
    JOIN questions qu ON qu.benchmark_id = s.benchmark_id AND qu.id = s.question_id
    WHERE {score_where_sql}
),
summary_cte AS (
    SELECT s.metric_name,
           count(*) FILTER (WHERE s.status = 'scored') AS scored,
           count(*) FILTER (WHERE s.status = 'not_applicable') AS not_applicable,
           count(*) FILTER (WHERE s.status = 'pending') AS pending,
           count(*) FILTER (WHERE s.status = 'error') AS error,
           avg(s.raw_value) FILTER (WHERE s.status = 'scored') AS mean
    FROM filtered_scores s
    GROUP BY s.metric_name
    ORDER BY s.metric_name
),
page_cte AS (
    SELECT s.question_id, s.ordinal, s.quota_bucket, s.metric_name, s.status,
           s.raw_value, s.numerator, s.denominator, s.reason_codes, s.submission_id
    FROM filtered_scores s
    {page_where_sql}
    ORDER BY s.ordinal, s.metric_name
    LIMIT %s
)
SELECT te.eval_id, te.grader_revision, te.eval_status, te.created_at,
       te.project_id, te.owner_actor_id,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
           'metric_name', metric_name,
           'scored', scored,
           'not_applicable', not_applicable,
           'pending', pending,
           'error', error,
           'mean', mean
       ) ORDER BY metric_name), '[]'::jsonb) FROM summary_cte),
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
           'question_id', question_id,
           'ordinal', ordinal,
           'quota_bucket', quota_bucket,
           'metric_name', metric_name,
           'status', status,
           'raw_value', raw_value,
           'numerator', numerator,
           'denominator', denominator,
           'reason_codes', to_jsonb(reason_codes),
           'submission_id', submission_id
       ) ORDER BY ordinal, metric_name), '[]'::jsonb) FROM page_cte)
FROM target_eval te
"""
    snapshot = conn.execute(sql, tuple(eval_args + [run_id] + score_args + page_args)).fetchone()
    if snapshot is None:
        raise missing
    eval_id, grader, ev_status, created_at, snapshot_project, snapshot_owner, summary_col, page_col = snapshot
    if eval_id is None:
        raise ApiError(404, "not_found", "evaluation not found")
    if snapshot_project != project_id:
        raise missing

    summary_data = json.loads(summary_col) if isinstance(summary_col, str) else (summary_col or [])
    page_data = json.loads(page_col) if isinstance(page_col, str) else (page_col or [])

    metrics_list = []
    for item in summary_data:
        if isinstance(item, dict):
            m, sc, na, pe, er, mean = (item["metric_name"], item["scored"], item["not_applicable"],
                                       item["pending"], item["error"], item["mean"])
        else:
            m, sc, na, pe, er, mean = item
        metrics_list.append({
            "metric_name": m,
            "scored": int(sc),
            "not_applicable": int(na),
            "pending": int(pe),
            "error": int(er),
            "mean": float(mean) if mean is not None else None,
        })

    has_more = len(page_data) > q["limit"]
    page = page_data[:q["limit"]]
    items_list = []
    for r in page:
        if isinstance(r, dict):
            qid, ord_, qb, mn, st, rv, num, den, rc, sub_id = (
                r["question_id"], r["ordinal"], r["quota_bucket"], r["metric_name"],
                r["status"], r["raw_value"], r["numerator"], r["denominator"],
                r["reason_codes"], r["submission_id"]
            )
        else:
            qid, ord_, qb, mn, st, rv, num, den, rc, sub_id = r
        items_list.append({
            "question_id": str(qid),
            "ordinal": int(ord_),
            "quota_bucket": qb,
            "metric_name": mn,
            "status": st,
            "raw_value": float(rv) if rv is not None else None,
            "numerator": float(num) if num is not None else None,
            "denominator": float(den) if den is not None else None,
            "reason_codes": list(rc) if rc is not None else [],
            "submission_id": str(sub_id) if sub_id else None,
        })

    # How the method's submissions ended (PM Q3): answered, a MODEL abstention (its own reason) and a METHOD failure (budget,
    # provider, contract, unknown outcome: a fixed code) are three different things and are counted separately.
    counts = {"answered": 0, "abstained_by_model": 0}
    method_failures: dict[str, int] = {}
    for status, failure, n in conn.execute(
            "SELECT answer_status, failure_reason, count(*) FROM submissions WHERE run_id = %s AND status = 'accepted' "
            "GROUP BY 1, 2", (run_id,)).fetchall():
        if failure is not None:
            method_failures[failure] = method_failures.get(failure, 0) + int(n)
        elif status == "answered":
            counts["answered"] += int(n)
        elif status == "abstained":
            counts["abstained_by_model"] += int(n)
    created_at_str = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    next_cursor = (
        encode_cursor(eval_id, items_list[-1]["ordinal"], items_list[-1]["metric_name"], filters=query_filters)
        if has_more and items_list
        else None
    )

    return {
        "run_id": str(run_id),
        "evaluation": {
            "id": str(eval_id),
            "grader_revision": grader,
            "status": ev_status,
            "created_at": created_at_str,
        },
        # Score-row coverage does not yet prove a full applicable-question
        # denominator. Even a completed evaluation is only a provisional
        # scored-subset statistic through this partial endpoint.
        "summary_scope": "scored_rows_only",
        "provisional": True,
        "filters": query_filters,
        "metrics": metrics_list,
        "submissions": {**counts, "method_failures": method_failures},
        "items": items_list,
        "next_cursor": next_cursor,
    }


async def get_results_handler(run_id: uuid.UUID, request: Request,
                              conn: psycopg.Connection = Depends(get_db),
                              actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    q = parse_query(request.query_params)
    content = await _run_blocking(request, "meta", _get_results, conn, actor_id, run_id, q)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/runs/{run_id}/results", get_results_handler, methods=["GET"], name="get_run_results_v1")
