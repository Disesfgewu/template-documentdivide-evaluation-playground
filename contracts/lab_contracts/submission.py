"""Run-bound external submission preflight (SPEC 7.3, 16.3).

Validation is not persistence or authorization. Callers must supply trusted run
context and commit each valid line under the durable run lock, audit and outbox.
The CLI only prints a payload-free diagnostic report; it never submits answers.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .base import Contract, ResourceId, Sha256, check_unique
from .method import (BatchRejected, CANONICAL_JSON_VERSION, MAX_JSONL_BYTES,
                     LineResult, QueryRequest, StrictJSONError, SubmissionLine, jsonl_lines,
                     parse_jsonl, strict_loads)

MAX_CONTEXT_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024


class DocumentContext(Contract):
    sha256: Sha256
    page_count: Annotated[int, Field(gt=0)]


class QueryContext(Contract):
    request: QueryRequest
    attempt_id: ResourceId


class RunContext(Contract):
    """Internal trusted metadata; contains public questions, never gold.

    Each attempt has an explicit query/request binding; IDs are never inferred
    from opaque identifiers. The surrounding application remains responsible for authorization and
    selecting the currently eligible attempt.
    """
    run_id: ResourceId
    benchmark_revision_id: ResourceId
    method_revision_id: ResourceId
    documents: Annotated[dict[ResourceId, DocumentContext], Field(min_length=1, max_length=100)]
    queries: Annotated[list[QueryContext], Field(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def _bindings(self):
        check_unique([(q.attempt_id, q.request.question_id) for q in self.queries], "attempt/question binding")
        if any(q.request.run_id != self.run_id for q in self.queries):
            raise ValueError("query run_id differs from context")
        return self


class InvalidRegion(Contract):
    hit_id: ResourceId
    region_index: Annotated[int, Field(ge=0)]
    reason_codes: list[str]


class PreflightLine(Contract):
    line: Annotated[int, Field(gt=0)]
    status: Literal["validated", "rejected"]
    reason_codes: list[str]
    question_id: ResourceId | None
    payload_hash: Sha256 | None
    duplicate_of: Annotated[int, Field(gt=0)] | None = None
    invalid_regions: list[InvalidRegion] = Field(default_factory=list)


@dataclass(frozen=True)
class CheckedSubmission:
    report: PreflightLine
    # Retain the original validated answer AND all source regions. Invalid
    # regions must be excluded from source scores, not independent answer scores.
    submission: SubmissionLine | None


def preflight_jsonl(body: bytes, context: RunContext) -> list[CheckedSubmission]:
    """Check each physical line against a trusted run context, without writes.

    Invalid encodings and batch bounds reject the entire batch. Other failures
    are isolated per line. An invalid first envelope cannot choose the context
    for later lines. Duplicate hashes apply only after all context checks pass.
    """
    context = load_context(context.model_dump_json(warnings=False).encode("utf-8"))
    lines = jsonl_lines(body)
    queries = {(q.attempt_id, q.request.question_id): q for q in context.queries}
    question_ids = {q.request.question_id for q in context.queries}
    hashes = {key: doc.sha256 for key, doc in context.documents.items()}
    seen: dict[tuple[str, str], tuple[str, int]] = {}
    out = []
    for n, raw in enumerate(lines, 1):
        if not raw.strip():
            parsed, sub = (LineResult(line=1, status="rejected", code="invalid_json",
                                      question_id=None, payload_hash=None), None)
        else:
            try:
                parsed, sub = parse_jsonl(raw.encode("utf-8"))[0]
            except (UnicodeError, BatchRejected):
                parsed, sub = (LineResult(line=1, status="rejected", code="invalid_json",
                                          question_id=None, payload_hash=None), None)
        reasons = []
        invalid = []
        duplicate = None
        if sub is None:
            reasons.append(parsed.code)
        else:
            for field in ("run_id", "benchmark_revision_id", "method_revision_id"):
                if getattr(sub, field) != getattr(context, field):
                    reasons.append(field + "_mismatch")
            if sub.document_hashes != hashes:
                reasons.append("document_hashes_mismatch")
            query = queries.get((sub.attempt_id, sub.response.question_id))
            if query is None:
                reasons.append("attempt_id_mismatch" if sub.response.question_id in question_ids else "unknown_question")
            else:
                reasons.extend(sub.response.check_binding(query.request))
            if not reasons:
                key = (sub.attempt_id, sub.response.question_id)
                if key in seen:
                    old_hash, first = seen[key]
                    if old_hash != parsed.payload_hash:
                        reasons.append("duplicate_in_batch")
                    else:
                        duplicate = first
                else:
                    seen[key] = (parsed.payload_hash, n)
            if not reasons:
                for hit in sub.response.retrieved_hits:
                    for index, region in enumerate(hit.regions):
                        doc = context.documents.get(region.document_id)
                        codes = (["unknown_document"] if doc is None else
                                 region.check_against_document(doc.sha256, doc.page_count))
                        if codes:
                            invalid.append(InvalidRegion(hit_id=hit.hit_id, region_index=index,
                                                         reason_codes=codes))
        report = PreflightLine(line=n, status="rejected" if reasons else "validated",
                               reason_codes=reasons, question_id=parsed.question_id,
                               payload_hash=parsed.payload_hash, duplicate_of=duplicate,
                               invalid_regions=invalid)
        out.append(CheckedSubmission(report, None if reasons else sub))
    return out


def read_regular(path: str, limit: int) -> bytes:
    """Read at most limit+1 bytes, rejecting special files before any read.

    O_NONBLOCK prevents FIFO open from hanging; fstat checks the actual opened
    descriptor, including symlink/race targets. Inputs are read-only.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("input_not_regular_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(limit + 1)
        if len(body) > limit:
            raise ValueError("input_too_large")
        return body
    finally:
        os.close(fd)


def load_context(body: bytes) -> RunContext:
    if len(body) > MAX_CONTEXT_BYTES:
        raise ValueError("context_too_large")
    try:
        text = body.decode("utf-8")
        strict_loads(text)
        return RunContext.model_validate_json(text)
    except (UnicodeError, StrictJSONError, ValidationError):
        raise ValueError("invalid_context") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate external JSONL against trusted run metadata; no submission is persisted")
    parser.add_argument("--context", required=True, help="trusted run-context JSON file (no gold)")
    parser.add_argument("--input", required=True, help="external JSONL regular file")
    args = parser.parse_args(argv)
    try:
        context = load_context(read_regular(args.context, MAX_CONTEXT_BYTES))
        checked = preflight_jsonl(read_regular(args.input, MAX_JSONL_BYTES), context)
    except BatchRejected as exc:
        print(json.dumps({"status": "rejected_batch", "code": exc.code}))
        return 2
    except (OSError, ValueError):
        # Never echo paths, raw input, Pydantic values, credentials or answers.
        print(json.dumps({"status": "rejected_batch", "code": "invalid_input_or_context"}))
        return 2
    reports = [row.report.model_dump(mode="json") for row in checked]
    rejected = sum(row.report.status == "rejected" for row in checked)
    encoded = json.dumps({"status": "preflight", "persisted": False,
                          "canonicalization": CANONICAL_JSON_VERSION,
                          "validated": len(checked) - rejected, "rejected": rejected,
                          "lines": reports}, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        print(json.dumps({"status": "rejected_batch", "code": "report_too_large"}))
        return 2
    print(encoded)
    return 1 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
