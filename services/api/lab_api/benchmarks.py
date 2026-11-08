"""Public question set of a benchmark revision (SPEC 8, 16.4).

``GET /api/v1/benchmarks/{logical_id}/revisions/{revision}/questions`` lists the
questions a caller may see without gold access. Answers, facts, evidence sets
and distractors are never selected, and only an explicit allow-list of the
stored question payload is copied out, so gold-like keys that ended up in a
payload cannot leave the service.
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
from pydantic import TypeAdapter, ValidationError

# .documents puts db/ and contracts/ on sys.path, so it is imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402
from lab_contracts.benchmark import Question  # noqa: E402

router = APIRouter()

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
BENCHMARK_RESOURCE_TYPE = "benchmark_revision"
# The only payload keys copied out (the public part of the Question contract).
PUBLIC_PAYLOAD_KEYS = ("language", "tags", "modality_requirements", "reasoning_type")
PUBLIC_FIELD_ADAPTERS = {name: TypeAdapter(Question.model_fields[name].rebuild_annotation())
                         for name in PUBLIC_PAYLOAD_KEYS}
MAX_ORDINAL = 2**31 - 1  # questions.ordinal is a PostgreSQL integer


def encode_cursor(benchmark_id: uuid.UUID, ordinal: int) -> str:
    raw = json.dumps({"b": str(benchmark_id), "o": ordinal}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, benchmark_id: uuid.UUID) -> int:
    """The last ordinal of the previous page. A cursor of another benchmark
    revision, or anything malformed, is a 400."""
    bad = ApiError(400, "invalid_cursor", "invalid cursor")
    try:
        if len(cursor) > 200:
            raise bad
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        raise bad from None
    if (not isinstance(data, dict) or set(data) != {"b", "o"} or data["b"] != str(benchmark_id)
            or not isinstance(data["o"], int) or isinstance(data["o"], bool) or not 0 <= data["o"] <= MAX_ORDINAL):
        raise bad
    return data["o"]


def parse_limit(raw: Optional[str]) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not raw.isascii() or not raw.isdigit():
        raise ApiError(400, "bad_request", f"limit must be an integer 1..{MAX_LIMIT}")
    normalized = raw.lstrip("0")
    if len(normalized) > len(str(MAX_LIMIT)) or not normalized or int(normalized) > MAX_LIMIT:
        raise ApiError(400, "bad_request", f"limit must be an integer 1..{MAX_LIMIT}")
    return int(normalized)


def public_question(row: tuple) -> dict:
    qid, key, ordinal, text, qhash, bucket, payload = row
    item = {"id": str(qid), "question_key": key, "ordinal": ordinal, "question_text": text,
            "question_hash": qhash, "quota_bucket": bucket}
    if isinstance(payload, dict):
        for name in PUBLIC_PAYLOAD_KEYS:
            if name in payload:
                adapter = PUBLIC_FIELD_ADAPTERS[name]
                try:
                    item[name] = adapter.dump_python(adapter.validate_python(payload[name]), mode="json")
                except ValidationError:
                    # Never include invalid stored values or validation details in the response.
                    raise ApiError(500, "invalid_question_payload", "invalid public question payload") from None
    return item


def _list_public_questions(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID,
                           logical_id: str, revision: int, cursor: Optional[str], limit: int) -> dict:
    try:
        decision = authorize(conn, project_id, actor_id, "read_licensed_pdf_and_public_questions")
        if decision.condition == "explicit_grant":
            raise PermissionDenied("observer requires explicit grant")
    except NotFound:
        raise ApiError(404, "not_found", "benchmark revision not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    try:
        authorize(conn, project_id, actor_id, "read_gold_and_generation_context")
        sees_unfrozen = True
    except PermissionDenied:
        sees_unfrozen = False

    row = conn.execute(
        "SELECT br.id, br.status::text FROM benchmark_revisions br JOIN resource_versions rv ON rv.id = br.resource_id "
        "WHERE br.project_id = %s AND rv.resource_type = %s AND rv.logical_id = %s AND rv.revision = %s",
        (project_id, BENCHMARK_RESOURCE_TYPE, logical_id, revision)).fetchone()
    # A caller without gold access gets the same 404 for a draft as for a missing revision.
    if row is None or (row[1] != "frozen" and not sees_unfrozen):
        raise ApiError(404, "not_found", "benchmark revision not found")
    benchmark_id, status = row
    after = decode_cursor(cursor, benchmark_id) if cursor else -1

    rows = conn.execute(
        "SELECT id, question_key, ordinal, question_text, question_hash, quota_bucket, payload FROM questions "
        "WHERE benchmark_id = %s AND ordinal > %s ORDER BY ordinal LIMIT %s",
        (benchmark_id, after, limit + 1)).fetchall()
    page = rows[:limit]
    return {
        "benchmark_revision_id": str(benchmark_id),
        "status": status,
        "items": [public_question(r) for r in page],
        "next_cursor": encode_cursor(benchmark_id, page[-1][2]) if len(rows) > limit else None,
    }


async def list_public_questions_handler(
    logical_id: str, revision: int, request: Request,
    conn: psycopg.Connection = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    project_id = resolve_project_id(request)
    limit = parse_limit(request.query_params.get("limit"))
    if not 1 <= len(logical_id) <= 128 or revision < 1 or revision > 2**31 - 1:
        raise ApiError(404, "not_found", "benchmark revision not found")
    content = await _run_blocking(request, "meta", _list_public_questions, conn, actor_id, project_id,
                                  logical_id, revision, request.query_params.get("cursor"), limit)
    return JSONResponse(status_code=200, content=content)


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions",
                     list_public_questions_handler, methods=["GET"], name="list_public_questions_v1")
