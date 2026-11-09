"""Run comparison (SPEC 8 ``GET /comparisons?run_ids=...``, SPEC 9.1, 14).

Each metric stays separate, 0-100; there is no weighted total and no ranking
(SPEC 9.1). The runs may only be compared when they share the benchmark
revision, the grader revision of their latest evaluation, the budget profile
and the set of questions that evaluation scored (SPEC 14 asks for the
"question subset"); per metric the scored questions must also be the same, so
two means are never compared over different questions, and no row may be
pending or errored. Runs in different statuses, or evaluations in different
states (provisional/published), are likewise not comparable. Otherwise the response is still 200 with
``comparable: false`` and every difference named, so a client can show the
reason instead of a misleading ranking. N/A and pending rows are counted, not
averaged as zero. As on the results endpoint the figures are over scored rows
only, hence ``provisional``.

Access follows the run-results rule for every run asked for (owner,
maintainer, reviewer; a participant's feedback profile is undefined); the
first run that fails authorization decides the response, so nothing is learned
that the single-run endpoint would not reveal.
"""
from __future__ import annotations

import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db
from .results import _authorize_results

router = APIRouter()
MIN_RUNS, MAX_RUNS = 2, 10

_SNAPSHOT = """
SELECT r.benchmark_id, r.method_revision_id, r.budget_profile, r.status::text,
       e.id, e.grader_revision, e.status, e.created_at,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'metric_name', m.metric_name, 'scored', m.scored, 'not_applicable', m.not_applicable,
                   'pending', m.pending, 'error', m.error, 'mean', m.mean,
                   'scored_hash', m.scored_hash) ORDER BY m.metric_name), '[]'::jsonb)
        FROM (SELECT metric_name,
                     count(*) FILTER (WHERE status = 'scored') AS scored,
                     count(*) FILTER (WHERE status = 'not_applicable') AS not_applicable,
                     count(*) FILTER (WHERE status = 'pending') AS pending,
                     count(*) FILTER (WHERE status = 'error') AS error,
                     avg(raw_value) FILTER (WHERE status = 'scored') AS mean,
                     encode(sha256(convert_to(coalesce(string_agg(question_id::text, ',' ORDER BY question_id)
                                                       FILTER (WHERE status = 'scored'), ''), 'UTF8')), 'hex') AS scored_hash
              FROM question_scores WHERE evaluation_id = e.id GROUP BY metric_name) m),
       (SELECT count(*) FROM (SELECT DISTINCT question_id FROM question_scores WHERE evaluation_id = e.id) d),
       (SELECT encode(sha256(convert_to(coalesce(string_agg(question_id::text, ',' ORDER BY question_id), ''),
                                        'UTF8')), 'hex')
        FROM (SELECT DISTINCT question_id FROM question_scores WHERE evaluation_id = e.id) d)
FROM runs r
LEFT JOIN LATERAL (SELECT id, grader_revision, status, created_at FROM evaluations
                   WHERE run_id = r.id ORDER BY created_at DESC, id DESC LIMIT 1) e ON true
WHERE r.id = %s
"""


def bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def parse_run_ids(params) -> list[uuid.UUID]:
    unknown = set(params) - {"run_ids"}
    if unknown:
        raise bad(f"unknown query parameter: {sorted(unknown)[0][:40]}")
    values = params.getlist("run_ids")
    if len(values) != 1:
        raise bad("run_ids is required once, as a comma-separated list")
    parts = values[0].split(",")
    if not MIN_RUNS <= len(parts) <= MAX_RUNS:
        raise bad(f"run_ids must list {MIN_RUNS}..{MAX_RUNS} runs")
    ids: list[uuid.UUID] = []
    for part in parts:
        try:
            ids.append(uuid.UUID(part))
        except ValueError:
            raise bad("run_ids must be UUIDs") from None
    if len(set(ids)) != len(ids):
        raise bad("run_ids must not repeat a run")
    return ids


def _run_entry(conn: psycopg.Connection, run_id: uuid.UUID) -> dict:
    row = conn.execute(_SNAPSHOT, (run_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run not found")
    bench, method, budget, run_status, ev_id, grader, ev_status, created, metrics, n_questions, subset = row
    entry = {
        "run_id": str(run_id), "method_revision_id": str(method), "benchmark_revision_id": str(bench),
        "budget_profile": budget, "run_status": run_status, "evaluation": None,
        "question_subset": None, "metrics": [],
    }
    if ev_id is not None:
        entry["evaluation"] = {"id": str(ev_id), "grader_revision": grader, "status": ev_status,
                               "created_at": created.isoformat()}
        entry["question_subset"] = {"count": int(n_questions), "hash": subset}
        entry["metrics"] = [{"metric_name": m["metric_name"], "scored": int(m["scored"]),
                             "not_applicable": int(m["not_applicable"]), "pending": int(m["pending"]),
                             "error": int(m["error"]), "scored_hash": m["scored_hash"],
                             "mean": float(m["mean"]) if m["mean"] is not None else None} for m in metrics]
    return entry


def blockers(entries: list[dict]) -> list[dict]:
    """The comparability conditions the runs do not share.

    Besides the identity of the benchmark, budget, grader and question subset,
    the figures themselves must be built the same way: the same metrics, over
    the same scored questions per metric, from evaluations in the same state,
    with nothing still pending or errored, and from runs in the same status.
    Otherwise two means over different questions would look comparable."""
    found: list[dict] = []
    missing = {e["run_id"]: "missing" for e in entries if e["evaluation"] is None}
    if missing:
        found.append({"condition": "evaluation", "values": missing})
    conditions = (
        ("benchmark_revision", lambda e: e["benchmark_revision_id"]),
        ("budget_profile", lambda e: e["budget_profile"]),
        ("run_status", lambda e: e["run_status"]),
        ("grader_revision", lambda e: e["evaluation"] and e["evaluation"]["grader_revision"]),
        ("evaluation_status", lambda e: e["evaluation"] and e["evaluation"]["status"]),
        ("question_subset", lambda e: e["question_subset"] and e["question_subset"]["hash"]),
    )
    for name, pick in conditions:
        values = {e["run_id"]: pick(e) for e in entries if pick(e) is not None}
        if len(set(values.values())) > 1:
            found.append({"condition": name, "values": values})
    evaluated = [e for e in entries if e["evaluation"] is not None]
    unfinished = {e["run_id"]: {"pending": sum(m["pending"] for m in e["metrics"]),
                                "error": sum(m["error"] for m in e["metrics"])}
                  for e in evaluated if any(m["pending"] or m["error"] for m in e["metrics"])}
    if unfinished:
        found.append({"condition": "unfinished_scoring", "values": unfinished})
    names = {e["run_id"]: [m["metric_name"] for m in e["metrics"]] for e in evaluated}
    if len({tuple(v) for v in names.values()}) > 1:
        found.append({"condition": "metric_set", "values": names})
    shared = set.intersection(*(set(v) for v in names.values())) if names else set()
    for metric in sorted(shared):
        per_run = {e["run_id"]: next(m for m in e["metrics"] if m["metric_name"] == metric) for e in evaluated}
        if len({m["scored_hash"] for m in per_run.values()}) > 1:
            found.append({"condition": "scored_question_set", "metric": metric,
                          "values": {r: {"scored": m["scored"], "hash": m["scored_hash"]} for r, m in per_run.items()}})
    return found


def _compare(conn: psycopg.Connection, actor_id: uuid.UUID, run_ids: list[uuid.UUID]) -> dict:
    for run_id in run_ids:
        _authorize_results(conn, actor_id, run_id)
    entries = [_run_entry(conn, run_id) for run_id in run_ids]
    found = blockers(entries)
    return {
        "run_ids": [str(r) for r in run_ids], "comparable": not found, "blockers": found,
        "summary_scope": "scored_rows_only", "provisional": True, "runs": entries,
    }


async def get_comparison_handler(request: Request, conn: psycopg.Connection = Depends(get_db),
                                 actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    run_ids = parse_run_ids(request.query_params)
    content = await _run_blocking(request, "meta", _compare, conn, actor_id, run_ids)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/comparisons", get_comparison_handler, methods=["GET"], name="get_comparison_v1")
