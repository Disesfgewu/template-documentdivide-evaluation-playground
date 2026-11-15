"""Two independent participant examples: raw-PDF page and text-window retrieval.

No system evidence, gold, model calls, or inferred geometry are consumed. The
UTF-8 byte budget is an example-only conservative tokenizer profile, not the
LAB's official scorer tokenizer. Preparation is in-memory and immutable.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
import threading
from contextlib import contextmanager
import re
import time
from dataclasses import asdict, dataclass
from importlib.metadata import version
from typing import Literal

from pypdf import PdfReader
from lab_contracts.method import (MAX_HITS, MAX_HIT_CONTENT_CHARS, MAX_QUERY_RESPONSE_BYTES,
                                  PrepareResult, QueryRequest, QueryResponse)
from lab_contracts.source import RegionKind, SourceRegion

REVISION = "local-lexical-1"
TOKENIZER = "utf8-bytes-1"
_PDF_LOG_LOCK = threading.RLock()


@contextmanager
def _pdf_diagnostics():
    """Treat native extraction warnings as incomplete, without leaking operands.

    pypdf reports some extraction errors only through logging and returns partial
    text. Filters run at each originating pypdf logger, before any app handler.
    Existing logger policy is restored; other threads' records pass unchanged.
    """
    with _PDF_LOG_LOCK:
        if logging.root.manager.disable >= logging.WARNING:
            raise ValueError("pdf_diagnostic_monitoring_unavailable")
        owner = threading.get_ident()
        warnings = [False]

        class Capture(logging.Filter):
            def filter(self, record):
                if record.thread == owner and record.levelno >= logging.WARNING:
                    warnings[0] = True  # bounded flag; never retain raw PDF operands
                    return False
                return True

        capture = Capture()
        # pypdf creates some loggers only at the first warning. Its pinned
        # logger_warning source is the already-loaded originating module name.
        names = {"pypdf"} | {name for name in (*sys.modules, *logging.Logger.manager.loggerDict)
                               if name.startswith("pypdf.")}
        loggers = [(logging.getLogger(name), logging.getLogger(name).level,
                    logging.getLogger(name).disabled) for name in names]
        try:
            for logger, _, _ in loggers:
                logger.filters.insert(0, capture)
                logger.disabled = False
                logger.setLevel(min(logger.getEffectiveLevel(), logging.WARNING))
            yield warnings
        finally:
            for logger, level, disabled in loggers:
                logger.removeFilter(capture)
                logger.setLevel(level)
                logger.disabled = disabled


@dataclass(frozen=True)
class MethodConfig:
    strategy: Literal["page", "window"] = "page"
    window_chars: int = 512
    max_pdf_bytes: int = 16 * 1024 * 1024
    max_pages: int = 2000
    max_page_chars: int = 32_000
    max_total_chars: int = 1_000_000
    max_chunks: int = 10_000

    def __post_init__(self):
        if self.strategy not in ("page", "window"):
            raise ValueError("unknown strategy")
        for name, ceiling in (("window_chars", MAX_HIT_CONTENT_CHARS),
                              ("max_pdf_bytes", 16 * 1024 * 1024),
                              ("max_pages", 2000), ("max_page_chars", MAX_HIT_CONTENT_CHARS),
                              ("max_total_chars", 1_000_000), ("max_chunks", 10_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    page_index: int
    start: int
    text: str


@dataclass(frozen=True)
class LocalIndex:
    document_id: str
    document_sha256: str
    page_count: int
    config: MethodConfig
    chunks: tuple[Chunk, ...]
    empty_pages: tuple[int, ...]
    parser_version: str
    fingerprint: str

    @property
    def index_id(self):
        return "local-" + self.fingerprint

    @property
    def method_revision(self) -> str:
        payload = {"implementation": REVISION, "config": asdict(self.config),
                   "parser": "pypdf", "parser_version": self.parser_version}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return REVISION + "-" + digest

    def manifest(self) -> PrepareResult:
        return PrepareResult(index_id=self.index_id, index_fingerprint=self.fingerprint,
                             document_hashes={self.document_id: self.document_sha256},
                             method_revision={"method_id": "local-" + self.config.strategy, "revision": self.method_revision},
                             status="ready", stats={"pages": self.page_count, "chunks": len(self.chunks),
                             "empty_native_text_pages": list(self.empty_pages), "tokenizer": TOKENIZER,
                             "parser": "pypdf", "parser_version": self.parser_version,
                             "local_retrieval_only": True})


def prepare(data: bytes, *, document_id: str, expected_sha256: str,
            config: MethodConfig = MethodConfig()) -> LocalIndex:
    """Parse participant-owned bytes. Reject partial/over-limit extraction.

    Page-sized chunks and nonoverlapping character windows are different
    algorithms. Both intentionally localize coarsely to the original full page.
    Native extraction can miss scanned/visual/cropped facts: never verified gold.
    """
    if not isinstance(data, bytes) or len(data) > config.max_pdf_bytes:
        raise ValueError("invalid or oversized PDF bytes")
    digest = hashlib.sha256(data).hexdigest()
    if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ValueError("invalid expected SHA-256")
    if digest != expected_sha256:
        raise ValueError("document_hash_mismatch")
    # Reuse the public type's identity checks, not system parsing/evidence.
    SourceRegion(document_id=document_id, document_sha256=digest, page_index=0,
                 bbox=(0., 0., 1., 1.), kind=RegionKind.page, quote=None)
    if not data.startswith(b"%PDF-"):
        raise ValueError("invalid_pdf")
    with _pdf_diagnostics() as diagnostics:
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ValueError("unsupported_encryption")
            count = len(reader.pages)
            if not 1 <= count <= config.max_pages:
                raise ValueError("page_limit_exceeded")
            chunks = []
            empty = []
            total = 0
            for page_index, page in enumerate(reader.pages):
                # pypdf text extraction includes off-crop content. Fail closed rather
                # than claim a visible full-page region supports invisible text.
                if tuple(page.cropbox) != tuple(page.mediabox):
                    raise ValueError("cropped_page_not_supported")
                text = page.extract_text() or ""
                total += len(text)
                if len(text) > config.max_page_chars or total > config.max_total_chars:
                    raise ValueError("native_text_limit_exceeded")
                if not text.strip():
                    empty.append(page_index)
                    continue
                step = len(text) if config.strategy == "page" else config.window_chars
                for start in range(0, len(text), step):
                    part = text[start:start + step]
                    if part.strip():
                        chunks.append(Chunk(f"p{page_index}-c{start}", page_index, start, part))
                        if len(chunks) > config.max_chunks:
                            raise ValueError("chunk_limit_exceeded")
        except ValueError:
            raise
        except Exception:
            raise ValueError("native_pdf_extraction_failed") from None
        if diagnostics[0]:
            raise ValueError("native_pdf_extraction_diagnostics")
    parser_version = version("pypdf")
    payload = {"revision": REVISION, "tokenizer": TOKENIZER, "config": asdict(config),
               "document_id": document_id, "document_sha256": digest, "page_count": count,
               "parser": "pypdf", "parser_version": parser_version,
               "chunks": [asdict(c) for c in chunks], "empty_pages": empty}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return LocalIndex(document_id, digest, count, config, tuple(chunks), tuple(empty), parser_version, fingerprint)


def _terms(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def query(index: LocalIndex, request: QueryRequest) -> QueryResponse:
    """Lexical intersection ranking, then the budget-limited ranked prefix.

    No answer synthesis: abstention preserves real retrieval evidence for source
    evaluation. Byte length counts every UTF-8 byte including whitespace. A hit
    that exceeds remaining budget ends the prefix; later hits never backfill it.
    """
    started = time.monotonic()
    if request.index_id != index.index_id:
        raise ValueError("index_id_mismatch")
    def check_deadline():
        if time.monotonic() - started >= request.budget.timeout_seconds:
            raise TimeoutError("query_deadline_exceeded")

    terms = _terms(request.question)
    scored = []
    for chunk in index.chunks:
        check_deadline()
        scored.append((len(terms & _terms(chunk.text)), chunk))
    ranked = sorted(scored, key=lambda pair: (-pair[0], pair[1].page_index, pair[1].start))
    check_deadline()
    hits = []
    spent = 0
    stopped = None
    for score, chunk in ranked:
        check_deadline()
        if score == 0 or len(hits) >= min(request.budget.top_k, MAX_HITS):
            break
        # A coarse kind=page region is visual under the shared protocol,
        # even when its content came from native text; count every hit.
        if len(hits) + 1 > request.budget.max_visual_regions:
            stopped = "visual_budget_prefix_exhausted"
            break
        cost = len(chunk.text.encode("utf-8"))
        if spent + cost > request.budget.max_evidence_tokens:
            stopped = "evidence_budget_prefix_exhausted"
            break
        spent += cost
        hits.append({"hit_id": chunk.chunk_id, "rank": len(hits) + 1, "content": chunk.text,
                     "regions": [{"document_id": index.document_id,
                                  "document_sha256": index.document_sha256,
                                  "page_index": chunk.page_index, "bbox": [0., 0., 1., 1.],
                                  "kind": "page", "quote": chunk.text}]})
    response = {"protocol_version": "1.0", "request_id": request.request_id,
                "question_id": request.question_id, "index_id": index.index_id,
                "status": "abstained", "answer": {"text": "", "claims": []},
                "reason": "retrieval_only_example_no_answer_generator", "retrieved_hits": hits,
                "citations": [], "telemetry": {"latency_ms": int((time.monotonic()-started)*1000),
                "retrieval_calls": 1, "model_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "retrieval_trace": [{"algorithm": "lexical-intersection-1", "tokenizer": TOKENIZER,
                                     "evidence_units_used": spent, "stop_reason": stopped,
                                     "empty_native_text_pages": list(index.empty_pages)}]}
    check_deadline()
    encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > MAX_QUERY_RESPONSE_BYTES:
        raise ValueError("query_response_limit_exceeded")
    result = QueryResponse.model_validate_json(encoded)
    check_deadline()
    return result
