"""A reviewed correctness per question of a run, next to the judge's value (Issue #218, PM-DECISION).

* ``POST /api/v1/runs/{run_id}/reviews``: a batch of up to 100 ``{question_id, value, reviewer_kind, reviewer_label, note}``; owner/maintainer.
  ``question_id`` is the question_key the details endpoint prints; ``value`` is 0-100; ``reviewer_kind`` is ``human`` or ``model_assisted``.
* ``GET  /api/v1/runs/{run_id}/reviews``: the latest review per question (with how many earlier ones there are) and the summary the web
  shows in step 9: the judge mean, the mean with the reviewed value where there is one, and how many questions were reviewed by whom.

The judge value in ``question_scores`` is never read for writing and never changed: a review is one more row of ``run_score_reviews`` (migration
0029, append-only, latest wins, history kept). Reviews are gold-side data: both endpoints need the build/gold permission, so a developer-side
endpoint (results, comparisons) never returns them. A model-assisted review is always labelled as such."""
from __future__ import annotations

import uuid
from typing import Annotated, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_review import BUILD_ACTION
from lab_db.repo import NotFound, PermissionDenied, authorize

router = APIRouter()
MAX_BATCH = 100
MAX_BODY_BYTES = 256 * 1024
METRIC = "correctness"


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    value: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    reviewer_kind: Literal["human", "model_assisted"]
    reviewer_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    note: Optional[Annotated[str, StringConstraints(max_length=500)]] = None


class ReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reviews: Annotated[list[ReviewItem], Field(min_length=1, max_length=MAX_BATCH)]


def _authorize_gold(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    """The benchmark id of the run; 404 for a run of another project, 403 unless the actor may read and write the gold side."""
    missing = ApiError(404, "not_found", "run not found")
    try:
        decision = authorize(conn, project_id, actor_id, BUILD_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if decision.condition is not None:
        raise ApiError(403, "permission_denied", f"{decision.role} may not read or write the reviewed scores of a run")
    row = conn.execute("SELECT benchmark_id FROM runs WHERE id = %s AND project_id = %s", (run_id, project_id)).fetchone()
    if row is None:
        raise missing
    return row[0]


def _latest(conn: psycopg.Connection, run_id: uuid.UUID) -> dict:
    out: dict = {}
    for qkey, qid, value, kind, label, note, actor, at, seq, n in conn.execute(
            "SELECT q.question_key, q.id, r.value, r.reviewer_kind, r.reviewer_label, r.note, r.actor_id, r.created_at, r.seq, "
            "(SELECT count(*) FROM run_score_reviews o WHERE o.run_id = r.run_id AND o.question_id = r.question_id AND o.metric = r.metric) "
            "FROM run_score_reviews r JOIN questions q ON q.benchmark_id = r.benchmark_id AND q.id = r.question_id "
            "WHERE r.run_id = %s AND r.metric = %s AND r.seq = (SELECT max(m.seq) FROM run_score_reviews m "
            "WHERE m.run_id = r.run_id AND m.question_id = r.question_id AND m.metric = r.metric)", (run_id, METRIC)):
        out[qid] = {"question_id": qkey, "value": float(value), "reviewer_kind": kind, "reviewer_label": label, "note": note,
                    "actor_id": str(actor), "reviewed_at": at.isoformat(), "versions": int(n)}
    return out


def _summary(conn: psycopg.Connection, run_id: uuid.UUID, latest: dict) -> dict:
    ev = conn.execute("SELECT id FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()
    judge: dict = {}
    if ev:
        judge = {qid: float(v) for qid, v in conn.execute(
            "SELECT question_id, raw_value FROM question_scores WHERE evaluation_id = %s AND metric_name = %s AND status = 'scored'", (ev[0], METRIC))}
    combined = {qid: (latest[qid]["value"] if qid in latest else v) for qid, v in judge.items()}
    for qid, r in latest.items():
        combined.setdefault(qid, r["value"])
    kinds = {"human": 0, "model_assisted": 0}
    for r in latest.values():
        kinds[r["reviewer_kind"]] += 1
    mean = lambda d: round(sum(d.values()) / len(d), 4) if d else None
    return {"metric": METRIC, "questions_reviewed": len(latest), "reviewed_by": kinds, "judge_scored": len(judge), "judge_mean": mean(judge),
            "reviewed_mean": mean(combined), "questions_in_reviewed_mean": len(combined),
            "rule": "the reviewed value where a question has one, otherwise the judge's value; questions the judge did not score count only if reviewed"}


def _list(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    _authorize_gold(conn, project_id, actor_id, run_id)
    latest = _latest(conn, run_id)
    return {"run_id": str(run_id), "items": sorted(latest.values(), key=lambda r: r["question_id"]), "summary": _summary(conn, run_id, latest),
            "contains_gold": True}


def _record(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, batch: ReviewBatch) -> dict:
    benchmark_id = _authorize_gold(conn, project_id, actor_id, run_id)
    keys = [r.question_id for r in batch.reviews]
    if len(set(keys)) != len(keys):
        raise ApiError(422, "invalid_reviews", "a question appears twice in one batch")
    ids = {k: q for k, q in conn.execute("SELECT question_key, id FROM active_questions WHERE benchmark_id = %s AND question_key = ANY(%s)",
                                         (benchmark_id, keys))}
    unknown = [k for k in keys if k not in ids]
    if unknown:
        raise ApiError(422, "invalid_reviews", f"{len(unknown)} question_id value(s) are not questions of this run's benchmark; nothing was recorded",
                       details={"unknown_question_ids": unknown[:20]})
    op_id = uuid.uuid4()
    try:
        with conn.transaction():
            conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) VALUES (%s, %s, 'run.score_review', %s, 'succeeded', now())",
                         (op_id, project_id, actor_id))
            for r in batch.reviews:
                conn.execute("INSERT INTO run_score_reviews (id, project_id, run_id, benchmark_id, question_id, metric, value, reviewer_kind, "
                             "reviewer_label, note, actor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                             (uuid.uuid4(), project_id, run_id, benchmark_id, ids[r.question_id], METRIC, r.value, r.reviewer_kind, r.reviewer_label,
                              r.note, actor_id))
            kinds = sorted({r.reviewer_kind for r in batch.reviews})
            conn.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, reason, outcome, operation_id) "
                         "VALUES (%s, %s, %s, 'run.score_reviewed', 'run', %s, %s, 'succeeded', %s)",
                         (uuid.uuid4(), project_id, actor_id, run_id, f"{len(batch.reviews)} reviewed correctness value(s) ({', '.join(kinds)})", op_id))
    except psycopg.errors.InsufficientPrivilege:
        raise ApiError(403, "permission_denied", "only a project owner or maintainer may record a reviewed score") from None
    latest = _latest(conn, run_id)
    return {"run_id": str(run_id), "recorded": len(batch.reviews), "summary": _summary(conn, run_id, latest),
            "note": "recorded next to the judge's values; nothing the judge scored was changed"}


async def list_handler(run_id: uuid.UUID, request: Request, conn: psycopg.Connection = Depends(get_db),
                       actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(await _run_blocking(request, "meta", _list, conn, actor_id, project_id, run_id))


async def record_handler(run_id: uuid.UUID, request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        batch = ReviewBatch.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", 'the body must be {"reviews": [{"question_id", "value": 0..100, "reviewer_kind": "human"|"model_assisted", '
                                           '"reviewer_label", "note"?}, ...]} with 1..100 reviews') from None
    return JSONResponse(status_code=201, content=await _run_blocking(request, "meta", _record, conn, actor_id, project_id, run_id, batch))


router.add_api_route("/api/v1/runs/{run_id}/reviews", list_handler, methods=["GET"], name="list_run_reviews_v1")
router.add_api_route("/api/v1/runs/{run_id}/reviews", record_handler, methods=["POST"], name="record_run_reviews_v1", status_code=201)
