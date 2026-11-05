"""PDF import validation service for lab_parser.

Implements SPEC R01 and section 16.6: validates uploaded bytes, determines
outcome ('accept', 'flag', 'reject'), page count, SHA-256 hash, and reason code.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

__all__ = ["Limits", "ImportResult", "inspect_pdf"]


@dataclass(frozen=True)
class Limits:
    """Resource bounds for PDF import validation (SPEC 16.6 defaults)."""

    max_bytes: int = 512 * 1024 * 1024
    max_pages: int = 2000


@dataclass(frozen=True)
class ImportResult:
    """Outcome and diagnostics for PDF import validation."""

    outcome: Literal["accept", "flag", "reject"]
    reason: str | None
    sha256: str
    size_bytes: int
    page_count: int | None
    warnings: tuple[str, ...] = ()


class _WarningCollector(logging.Handler):
    """Temporary logging handler to collect warnings emitted by pypdf."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.warnings.append(record.getMessage())
        except Exception:
            self.handleError(record)


def inspect_pdf(data: bytes, limits: Limits = Limits()) -> ImportResult:
    """Inspect and validate PDF bytes.

    Validation steps (evaluated strictly in order):
    1. size_bytes > limits.max_bytes -> reject "file_too_large" (no parsing).
    2. size_bytes == 0 -> reject "empty_file".
    3. No '%PDF-' found within first 1024 bytes (must be file start, allowing
       only leading whitespace) -> reject "not_a_pdf".
    4. pypdf strict parsing failure (PdfReadError, etc.) -> reject "corrupt_pdf".
    5. Encrypted -> reject "encrypted_pdf" (do not guess password).
    6. page_count > limits.max_pages -> reject "page_limit_exceeded" (page_count recorded).
    7. pypdf emitted warning containing "repaired" -> outcome "flag", reason "xref_repaired".
    8. Otherwise -> accept, reason None.

    sha256 and size_bytes are always computed from raw input bytes.
    Document text content is treated strictly as data and does not alter determination.
    """
    if limits is None:
        limits = Limits()

    sha256 = hashlib.sha256(data).hexdigest()
    size_bytes = len(data)

    # 1. 超過 max_bytes → reject "file_too_large"（不要解析）。
    if size_bytes > limits.max_bytes:
        return ImportResult(
            outcome="reject",
            reason="file_too_large",
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=None,
            warnings=(),
        )

    # 2. 空 → reject "empty_file"。
    if size_bytes == 0:
        return ImportResult(
            outcome="reject",
            reason="empty_file",
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=None,
            warnings=(),
        )

    # 3. 前 1024 bytes 內找不到 `%PDF-` 開頭（必須是檔案開頭，允許前面只有空白）→ reject "not_a_pdf"。
    head = data[:1028]
    stripped = head.lstrip(b" \t\r\n\f\v\0")
    leading_ws = len(head) - len(stripped)
    if leading_ws >= 1024 or not stripped.startswith(b"%PDF-"):
        return ImportResult(
            outcome="reject",
            reason="not_a_pdf",
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=None,
            warnings=(),
        )

    # Attach temporary warning collector to pypdf logger
    pypdf_logger = logging.getLogger("pypdf")
    collector = _WarningCollector()
    pypdf_logger.addHandler(collector)
    old_level = pypdf_logger.level
    if pypdf_logger.level == logging.NOTSET or pypdf_logger.level > logging.WARNING:
        pypdf_logger.setLevel(logging.WARNING)

    try:
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            # 5. 加密 → reject "encrypted_pdf"（不嘗試猜密碼）。
            if reader.is_encrypted:
                return ImportResult(
                    outcome="reject",
                    reason="encrypted_pdf",
                    sha256=sha256,
                    size_bytes=size_bytes,
                    page_count=None,
                    warnings=tuple(collector.warnings),
                )
            page_count = len(reader.pages)
        except FileNotDecryptedError:
            # 5. 加密 → reject "encrypted_pdf"
            return ImportResult(
                outcome="reject",
                reason="encrypted_pdf",
                sha256=sha256,
                size_bytes=size_bytes,
                page_count=None,
                warnings=tuple(collector.warnings),
            )
        except (PdfReadError, Exception):
            # 4. pypdf strict 解析失敗（PdfReadError 等）→ reject "corrupt_pdf"。
            return ImportResult(
                outcome="reject",
                reason="corrupt_pdf",
                sha256=sha256,
                size_bytes=size_bytes,
                page_count=None,
                warnings=tuple(collector.warnings),
            )

        warnings_tuple = tuple(collector.warnings)

        # 6. 頁數 > max_pages → reject "page_limit_exceeded"（page_count 填實際頁數）。
        if page_count > limits.max_pages:
            return ImportResult(
                outcome="reject",
                reason="page_limit_exceeded",
                sha256=sha256,
                size_bytes=size_bytes,
                page_count=page_count,
                warnings=warnings_tuple,
            )

        # 7. 解析過程中 pypdf 發出含 "repaired" 的警告 → outcome "flag"、reason "xref_repaired"。
        has_repaired = any("repaired" in w.lower() for w in warnings_tuple)
        if has_repaired:
            return ImportResult(
                outcome="flag",
                reason="xref_repaired",
                sha256=sha256,
                size_bytes=size_bytes,
                page_count=page_count,
                warnings=warnings_tuple,
            )

        # 8. 其他 → accept，reason None。
        return ImportResult(
            outcome="accept",
            reason=None,
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=page_count,
            warnings=warnings_tuple,
        )

    finally:
        pypdf_logger.removeHandler(collector)
        pypdf_logger.setLevel(old_level)
