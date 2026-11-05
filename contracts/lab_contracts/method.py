"""Method / agent integration protocol (SPEC 7.1-7.3, 16.3).

HTTP method services and external JSONL submissions share one query response
schema. Structural problems raise ValidationError (API: 422); a body that is
not JSON at all is 400 (see ``parse_query_response``). Source regions that are
structurally valid but point at a wrong page/hash are *not* rejected here:
they are kept and flagged invalid_region by the scorer (SPEC 16.3, T35).
"""
from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from .api import ErrorBody
from .base import Contract, ResourceId, Sha256, check_unique
from .source import RegionKind, SourceRegion

PROTOCOL_VERSION = "1.0"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION})

# Input limits (bounded so a single request cannot exhaust memory/cost).
MAX_DOCUMENTS_PER_PREPARE = 100
MAX_QUESTION_CHARS = 8_000
MAX_ANSWER_CHARS = 32_000
MAX_HIT_CONTENT_CHARS = 32_000
MAX_HITS = 200
MAX_REGIONS_PER_HIT = 64
MAX_CLAIMS = 200
MAX_CITATIONS = 1_000
MAX_JSONL_BYTES = 16 * 1024 * 1024        # SPEC 16.6: JSONL batch <= 100 lines and 16 MiB
MAX_JSONL_LINES = 100
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024   # a single line can use the whole batch budget
MAX_QUERY_RESPONSE_BYTES = 16 * 1024 * 1024  # SPEC 16.6: JSON payload 16 MiB
MAX_TRACE_ENTRIES = 1_000

def _supported_version(v: str) -> str:
    if v not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(f"unsupported protocol_version (supported: {sorted(SUPPORTED_PROTOCOL_VERSIONS)})")
    return v


ProtocolVersion = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+$"),
                            AfterValidator(_supported_version)]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]
# Non-empty and not whitespace-only.
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


_unique = check_unique


# --- 7.1 identity / capabilities -------------------------------------------------

class Modality(str, Enum):
    text = "text"
    table = "table"
    image = "image"


class MethodRevisionRef(Contract):
    method_id: ResourceId
    revision: ResourceId


class Capabilities(Contract):
    """GET /v1/capabilities."""

    protocol_version: ProtocolVersion
    method_id: ResourceId
    revision: ResourceId
    supported_modalities: Annotated[list[Modality], Field(min_length=1)]
    async_query: bool
    multi_region: bool
    max_parallel_queries: PosInt

    @model_validator(mode="after")
    def _distinct(self):
        _unique(self.supported_modalities, "modality")
        return self


# --- prepare ---------------------------------------------------------------------

class PrepareDocument(Contract):
    document_id: ResourceId
    sha256: Sha256  # of the raw PDF bytes; the method verifies after download
    page_count: PosInt
    pdf_url: Annotated[str, StringConstraints(pattern=r"^https?://", max_length=4096)]
    url_expires_at: AwareDatetime


class PrepareRequest(Contract):
    """POST /v1/prepare -> 202 AsyncAccepted."""

    protocol_version: ProtocolVersion
    request_id: ResourceId
    documents: Annotated[list[PrepareDocument], Field(min_length=1, max_length=MAX_DOCUMENTS_PER_PREPARE)]
    method_revision: MethodRevisionRef
    config: dict[str, Any]
    config_hash: Sha256

    @model_validator(mode="after")
    def _distinct_documents(self):
        _unique([d.document_id for d in self.documents], "document_id")
        return self


class PrepareResult(Contract):
    """Terminal result of a succeeded prepare job."""

    index_id: ResourceId
    index_fingerprint: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    document_hashes: Annotated[dict[ResourceId, Sha256], Field(min_length=1)]
    method_revision: MethodRevisionRef
    status: Literal["ready"]
    stats: Optional[dict[str, Any]] = None

    def check_against_request(self, req: PrepareRequest) -> list[str]:
        """Reasons the result does not match the prepare request (empty = ok)."""
        reasons = []
        expected = {d.document_id: d.sha256 for d in req.documents}
        if self.document_hashes != expected:
            reasons.append("document_hashes_mismatch")
        if self.method_revision != req.method_revision:
            reasons.append("method_revision_mismatch")
        return reasons


# --- query -----------------------------------------------------------------------

class QueryBudget(Contract):
    top_k: PosInt
    max_evidence_tokens: PosInt
    max_visual_regions: NonNegInt
    timeout_seconds: PosInt


class QueryRequest(Contract):
    """POST /v1/query -> 200 QueryResponse or 202 AsyncAccepted."""

    protocol_version: ProtocolVersion
    request_id: ResourceId
    run_id: ResourceId
    question_id: ResourceId
    index_id: ResourceId
    question: Annotated[str, StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARS)]
    budget: QueryBudget


class Claim(Contract):
    claim_id: ResourceId
    text: Text


class Answer(Contract):
    text: Annotated[str, StringConstraints(max_length=MAX_ANSWER_CHARS)]
    claims: Annotated[list[Claim], Field(max_length=MAX_CLAIMS)]


VISUAL_KINDS = frozenset({RegionKind.image, RegionKind.figure, RegionKind.table, RegionKind.page,
                          RegionKind.formula})


class Hit(Contract):
    hit_id: ResourceId
    rank: PosInt
    # May be "" only for a pure visual hit; LAB re-crops from the original PDF.
    content: Annotated[str, StringConstraints(max_length=MAX_HIT_CONTENT_CHARS)]
    regions: Annotated[list[SourceRegion], Field(min_length=1, max_length=MAX_REGIONS_PER_HIT)]

    @model_validator(mode="after")
    def _empty_content_needs_visual_regions(self):
        # PM decision (#4, 2026-11-05): an image hit may also carry the text
        # regions it needs (caption, conditions); empty content only requires
        # at least one visual region LAB can re-crop from the original PDF.
        # Whitespace-only content counts as empty. This is a structural rule,
        # not proof that the geometry semantically supports the answer.
        if not self.content.strip() and not any(r.kind in VISUAL_KINDS for r in self.regions):
            raise ValueError("empty content requires at least one visual region (image/figure/table/page/formula)")
        return self


class Citation(Contract):
    """region_indices may repeat (PM #4 Q1): accepted as submitted, and
    ``normalized_region_indices`` gives the de-duplicated order-preserving
    form scorers must use, so repeats never add coverage or dilute errors.
    The payload hash is always computed over the original submission."""

    claim_id: ResourceId
    hit_id: ResourceId
    region_indices: Annotated[list[NonNegInt], Field(min_length=1)]

    @property
    def normalized_region_indices(self) -> list[int]:
        return list(dict.fromkeys(self.region_indices))


class Telemetry(Contract):
    """Method self-report. Every key is required; unknown values are null
    (0 means a confirmed zero, SPEC 16.1)."""

    latency_ms: Optional[NonNegInt]
    retrieval_calls: Optional[NonNegInt]
    model_calls: Optional[NonNegInt]
    input_tokens: Optional[NonNegInt]
    output_tokens: Optional[NonNegInt]


class QueryResponse(Contract):
    protocol_version: ProtocolVersion
    request_id: ResourceId
    question_id: ResourceId
    index_id: ResourceId
    status: Literal["answered", "abstained"]
    answer: Answer
    reason: Optional[Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r"\S")]] = None
    retrieved_hits: Annotated[list[Hit], Field(max_length=MAX_HITS)]
    citations: Annotated[list[Citation], Field(max_length=MAX_CITATIONS)]
    telemetry: Optional[Telemetry] = None
    retrieval_trace: Optional[Annotated[list[dict[str, Any]], Field(max_length=MAX_TRACE_ENTRIES)]] = None

    @model_validator(mode="after")
    def _semantics(self):
        if self.status == "answered":
            if not self.answer.text.strip():
                raise ValueError("answered requires non-empty answer.text")
        else:
            if self.reason is None:
                raise ValueError("abstained requires reason")
            if self.answer.text != "" or self.answer.claims:
                raise ValueError("abstained requires empty answer.text and claims=[]")

        _unique([c.claim_id for c in self.answer.claims], "claim_id")
        _unique([h.hit_id for h in self.retrieved_hits], "hit_id")
        ranks = sorted(h.rank for h in self.retrieved_hits)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("ranks must be exactly 1..n without duplicates or gaps")

        claims = {c.claim_id for c in self.answer.claims}
        hits = {h.hit_id: h for h in self.retrieved_hits}
        for i, cit in enumerate(self.citations):
            if cit.claim_id not in claims:
                raise ValueError(f"citations[{i}].claim_id does not exist")
            hit = hits.get(cit.hit_id)
            if hit is None:
                raise ValueError(f"citations[{i}].hit_id does not exist")
            # Repeated indices are allowed; the scorer de-duplicates (SPEC 16.5).
            if any(ix >= len(hit.regions) for ix in cit.region_indices):
                raise ValueError(f"citations[{i}].region_indices out of range for hit")
        return self

    def check_binding(self, req: QueryRequest) -> list[str]:
        """HTTP responses must echo the request identity (SPEC 16.3)."""
        return [f"{f}_mismatch" for f in ("request_id", "question_id", "index_id")
                if getattr(self, f) != getattr(req, f)]


# --- async jobs ------------------------------------------------------------------

class AsyncAccepted(Contract):
    """202 body for prepare and async query."""

    job_id: ResourceId
    poll_after_ms: NonNegInt


class JobStatus(Contract):
    """GET /v1/jobs/{job_id}. Only succeeded carries a terminal result;
    failed carries error. HTTP 200 only means the lookup itself succeeded."""

    job_id: ResourceId
    kind: Literal["prepare", "query"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: Optional[Annotated[float, Field(ge=0.0, le=1.0)]]
    result: Optional[Union[PrepareResult, QueryResponse]] = None
    error: Optional[ErrorBody] = None

    @model_validator(mode="after")
    def _terminal(self):
        if self.status == "succeeded":
            if self.result is None:
                raise ValueError("succeeded requires result")
            want = PrepareResult if self.kind == "prepare" else QueryResponse
            if not isinstance(self.result, want):
                raise ValueError(f"{self.kind} job result must be {want.__name__}")
        elif self.result is not None:
            raise ValueError("only succeeded jobs carry result")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("error is required for failed and forbidden otherwise")
        return self


# --- 7.3 external JSONL submission ------------------------------------------------

class SubmissionLine(Contract):
    run_id: ResourceId
    attempt_id: ResourceId
    benchmark_revision_id: ResourceId
    method_revision_id: ResourceId
    document_hashes: Annotated[dict[ResourceId, Sha256], Field(min_length=1)]
    response: QueryResponse


CANONICAL_JSON_VERSION = "lab-c14n-1"
_MAX_SAFE_INT = 2 ** 53


class StrictJSONError(ValueError):
    """Body is not strict JSON: syntax, duplicate keys, NaN/Infinity, nesting."""


def _reject_constant(name: str):
    raise StrictJSONError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    v = float(text)
    if not math.isfinite(v):  # e.g. 1e999 overflows to inf
        raise StrictJSONError("number out of range")
    return v


def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise StrictJSONError(f"duplicate key {k!r}")
        out[k] = v
    return out


def strict_loads(text: str) -> Any:
    """json.loads that rejects duplicate keys, NaN/Infinity and overflowing
    numbers, and turns pathological nesting into StrictJSONError."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError:
        raise StrictJSONError("nesting too deep") from None
    except StrictJSONError:
        raise
    except ValueError as exc:
        raise StrictJSONError(f"invalid JSON ({type(exc).__name__})") from None


def _normalize(v: Any) -> Any:
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        # 1 and 1.0 are the same JSON number: integral floats hash as integers.
        return int(v) if v.is_integer() and abs(v) < _MAX_SAFE_INT else v
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    if isinstance(v, dict):
        return {k: _normalize(x) for k, x in v.items()}
    raise TypeError(f"not a JSON value: {type(v).__name__}")


def canonical_json(value: Any) -> bytes:
    """lab-c14n-1 (Python-defined, not RFC 8785 JCS): keys sorted by code
    point, no whitespace, UTF-8, integral floats written as integers, other
    floats as Python's shortest round-trip repr, non-finite numbers forbidden.
    Changing any of this requires a new CANONICAL_JSON_VERSION."""
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def payload_hash(raw_value: Any) -> str:
    """Hash of the submitted JSON value exactly as sent (after strict parsing),
    not of a model dump, so adding optional model fields never changes the
    hash of an existing payload."""
    return hashlib.sha256(canonical_json(raw_value)).hexdigest()


class LineResult(Contract):
    line: PosInt
    status: Literal["accepted", "rejected"]
    code: Optional[Literal["invalid_json", "line_too_large", "schema_invalid",
                           "duplicate_in_batch", "envelope_mismatch"]]
    question_id: Optional[ResourceId]
    payload_hash: Optional[Sha256]
    # Set on an identical resend inside the same batch: the earlier line that
    # carries the same payload, so callers do not persist it twice.
    duplicate_of: Optional[PosInt] = None


class BatchRejected(Exception):
    """The whole JSONL body is rejected (bad encoding or size; SPEC 16.3)."""

    def __init__(self, http_status: int, code: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code


_UTF8_BOM = "\ufeff"


def _rejected(n: int, code: str, qid: Optional[str] = None, h: Optional[str] = None) -> LineResult:
    return LineResult(line=n, status="rejected", code=code, question_id=qid, payload_hash=h)


def parse_jsonl(body: bytes) -> list[tuple[LineResult, Optional[SubmissionLine]]]:
    """Validate an external submission. Each line is accepted or rejected on
    its own (T33); blank lines inside the body are rejected as invalid_json.
    All accepted lines of one batch must share run/benchmark/method/document
    binding. Persisting, idempotency against stored payloads (same hash ->
    same result, different hash -> 409) and the run lock are #5+."""
    if len(body) > MAX_JSONL_BYTES:
        raise BatchRejected(413, "payload_too_large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise BatchRejected(415, "invalid_encoding") from None
    if text.startswith(_UTF8_BOM):
        raise BatchRejected(415, "invalid_encoding")
    if not text.strip():
        raise BatchRejected(422, "empty_submission")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_JSONL_LINES:
        raise BatchRejected(413, "too_many_lines")

    out: list[tuple[LineResult, Optional[SubmissionLine]]] = []
    seen: dict[tuple[str, str], tuple[str, int]] = {}
    envelope = None
    for n, raw in enumerate(lines, start=1):
        raw = raw.removesuffix("\r")
        if len(raw.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
            out.append((_rejected(n, "line_too_large"), None))
            continue
        try:
            value = strict_loads(raw)
        except StrictJSONError:
            out.append((_rejected(n, "invalid_json"), None))
            continue
        try:
            sub = SubmissionLine.model_validate_json(raw)
        except ValidationError:
            out.append((_rejected(n, "schema_invalid"), None))
            continue
        qid = sub.response.question_id
        h = payload_hash(value)
        env = (sub.run_id, sub.benchmark_revision_id, sub.method_revision_id, sub.document_hashes)
        if envelope is not None and env != envelope:
            out.append((_rejected(n, "envelope_mismatch", qid, h), None))
            continue
        key = (sub.attempt_id, qid)
        if key in seen:
            first_hash, first_line = seen[key]
            if first_hash != h:
                out.append((_rejected(n, "duplicate_in_batch", qid, h), None))
                continue
            out.append((LineResult(line=n, status="accepted", code=None, question_id=qid,
                                   payload_hash=h, duplicate_of=first_line), sub))
            continue
        envelope = env
        seen[key] = (h, n)
        out.append((LineResult(line=n, status="accepted", code=None, question_id=qid,
                               payload_hash=h), sub))
    return out


def parse_query_response(body: bytes, request: Optional[QueryRequest] = None
                         ) -> tuple[int, Optional[QueryResponse], list[str]]:
    """(HTTP status, model, reasons) for a method's query body:
    413 too large, 400 not strict UTF-8 JSON, 422 schema/semantic error or a
    response that does not echo the request identity, 200 valid."""
    if len(body) > MAX_QUERY_RESPONSE_BYTES:
        return 413, None, ["payload_too_large"]
    try:
        strict_loads(body.decode("utf-8"))
    except UnicodeDecodeError:
        return 400, None, ["invalid_encoding"]
    except StrictJSONError:
        return 400, None, ["invalid_json"]
    try:
        resp = QueryResponse.model_validate_json(body)
    except ValidationError:
        return 422, None, ["schema_invalid"]
    if request is not None:
        mismatch = resp.check_binding(request)
        if mismatch:
            return 422, None, mismatch
    return 200, resp, []
