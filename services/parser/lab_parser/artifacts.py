"""Bounded local original-PDF ingestion into the content-addressed artifact store.

Opens a regular local PDF without following symlinks and hands it to the
store's ``put_stream``, which stages, hashes and publishes it (the single
publisher for the ``sha256/<xx>/<sha256>`` layout; this module has no second
copy of staging, verification or publication). Before anything is published a
hook inspects the staged bytes in the resource-limited isolation child
(``lab_parser.isolation.inspect_geometry``: strict pypdf, encryption, page limit,
and a PDFium page-count cross-check under RLIMIT memory/CPU and a wall deadline),
so a rejected PDF never reaches the store and untrusted structure never parses in
the caller's process.

The ``store`` argument is any object with ``root`` and ``put_stream(fd,
max_bytes=..., inspect=...)``, in practice ``lab_db.storage.LocalArtifactStore``;
it is passed in so the parser does not import the database package.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Literal

from .importer import ImportResult, Limits
from .isolation import InspectionError, ProcessLimits, inspect_geometry

__all__ = [
    "OBJECT_VERSION",
    "PdfArtifactError",
    "IngestedPdfArtifact",
    "ingest_pdf_artifact",
    "inspect_pdf_isolated",
]

OBJECT_VERSION = "1"
_HEAD_BYTES = 2048

class PdfArtifactError(ValueError):
    """Fixed error code for local PDF artifact ingestion failures.

    The error string is always a sanitized reason code and never embeds raw PDF
    content, unfiltered pypdf diagnostics, or filesystem paths.
    """

    def __init__(
        self,
        code: str,
        *,
        inspection: ImportResult | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.inspection = inspection


@dataclass(frozen=True)
class IngestedPdfArtifact:
    """Published content-addressed original PDF artifact metadata."""

    outcome: Literal["accept"]
    reason: str | None
    sha256: str
    size_bytes: int
    page_count: int
    object_key: str
    object_version: str
    artifact_path: Path
    reused_existing: bool
    warnings: tuple[str, ...] = ()


def _validate_path_arg(value: str | Path, error_code: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise PdfArtifactError(error_code)
    try:
        path = Path(value)
        raw = os.fsencode(path)
    except (TypeError, ValueError):
        raise PdfArtifactError(error_code) from None
    if len(value if isinstance(value, str) else path.parts) == 0 or len(raw) == 0 or b"\0" in raw:
        raise PdfArtifactError(error_code)
    return path


def _validate_limits(limits: Limits | None) -> Limits:
    if limits is None:
        return Limits()
    if not isinstance(limits, Limits):
        raise PdfArtifactError("invalid_limits")
    try:
        return Limits(max_bytes=limits.max_bytes, max_pages=limits.max_pages)
    except (TypeError, ValueError):
        raise PdfArtifactError("invalid_limits") from None


def _open_regular_nofollow(
    path: Path | str,
    *,
    not_regular_code: str,
    unavailable_code: str,
    dir_fd: int | None = None,
) -> int:
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
        st = os.fstat(fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            raise PdfArtifactError(not_regular_code) from None
        raise PdfArtifactError(unavailable_code) from None

    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise PdfArtifactError(not_regular_code)
    return fd


_CHILD_CODES = {
    "invalid_pdf": "corrupt_pdf",
    "encrypted_pdf": "encrypted_pdf",
    "page_limit_exceeded": "page_limit_exceeded",
    "timeout": "inspection_timeout",
    "cancelled": "inspection_cancelled",
    "memory_limit": "inspection_failed",
}


def _process_limits(size_bytes: int, limits: Limits) -> ProcessLimits:
    """Scale the child's budget with the file, inside the isolation module's bounds."""
    mebibytes = size_bytes // (1024 * 1024)
    memory = min(4 * 1024 ** 3, max(512 * 1024 ** 2, 4 * size_bytes + 256 * 1024 ** 2))
    cpu = min(120, 5 + mebibytes // 8)
    return ProcessLimits(
        memory_bytes=memory,
        cpu_seconds=cpu,
        wall_seconds=min(180, 2 * cpu),
        max_pdf_bytes=max(1, min(limits.max_bytes, 512 * 1024 * 1024)),
        max_pages=min(limits.max_pages, 2000),
    )


def _read_staged(stage_fd: int, size_bytes: int) -> bytes:
    chunks, offset = [], 0
    while offset < size_bytes:
        chunk = os.pread(stage_fd, min(1 << 20, size_bytes - offset), offset)
        if not chunk:
            raise PdfArtifactError("artifact_corrupted")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _cheap_reject(size_bytes: int, head: bytes, limits: Limits) -> str | None:
    """Reason for the checks that need no PDF parsing, in the importer's order."""
    if size_bytes > limits.max_bytes:
        return "file_too_large"
    if size_bytes == 0:
        return "empty_file"
    stripped = head.lstrip(b" \t\r\n\f\v\0")
    if len(head) - len(stripped) >= 1024 or not stripped.startswith(b"%PDF-"):
        return "not_a_pdf"
    return None


def _run_child(data: bytes, sha256: str, limits: Limits, cancelled=None) -> tuple[ImportResult, tuple]:
    size_bytes = len(data)
    try:
        inspection = inspect_geometry(data, _process_limits(size_bytes, limits), cancelled=cancelled)
    except InspectionError as exc:
        return ImportResult(outcome="reject", reason=_CHILD_CODES.get(exc.code, "inspection_failed"),
                            sha256=sha256, size_bytes=size_bytes, page_count=None, warnings=()), ()
    return ImportResult(outcome="accept", reason=None, sha256=sha256, size_bytes=size_bytes,
                        page_count=inspection.page_count, warnings=()), inspection.pages


def inspect_pdf_isolated(data: bytes, limits: Limits, cancelled=None) -> tuple[ImportResult, tuple]:
    """Inspect PDF bytes in the isolation child; one inspector for ingest and upload.

    Returns ``(result, pages)``: a reject result (fixed reason code) with no pages,
    or an accept result whose ``page_count`` is the number pypdf, the geometry
    extractor and PDFium agree on, with the child's per-page geometry and label.
    ``cancelled`` is an optional fast, nonblocking callback (for example a client disconnect
    flag); when it turns true the child is stopped and the result is ``inspection_cancelled``.
    Never raises for a bad PDF; only the cheap checks run in the caller process.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    reason = _cheap_reject(len(data), data[:_HEAD_BYTES], limits)
    if reason is not None:
        return ImportResult(outcome="reject", reason=reason, sha256=sha256, size_bytes=len(data),
                            page_count=None, warnings=()), ()
    return _run_child(data, sha256, limits, cancelled)


def _inspect_staged_fd(
    stage_fd: int,
    *,
    sha256: str,
    size_bytes: int,
    head: bytes,
    limits: Limits,
) -> ImportResult:
    reason = _cheap_reject(size_bytes, head, limits)
    if reason is not None:
        return ImportResult(outcome="reject", reason=reason, sha256=sha256, size_bytes=size_bytes,
                            page_count=None, warnings=())
    try:
        data = _read_staged(stage_fd, size_bytes)
    except OSError:
        raise PdfArtifactError("artifact_root_unavailable") from None
    return _run_child(data, sha256, limits)[0]


def ingest_pdf_artifact(
    source: str | Path,
    store: Any,
    *,
    limits: Limits | None = None,
) -> IngestedPdfArtifact:
    """Stream, strictly validate, and publish a regular local PDF.

    Args:
        source: Path to the local regular PDF file (symlinks/FIFOs rejected).
        store: The artifact store (``LocalArtifactStore``) that publishes it.
        limits: Byte and page bounds (defaults to SPEC 16.6: 512 MiB, 2000 pages).

        A PDF the isolation child cannot read cleanly (including any repaired
        xref or other pypdf warning) is rejected as ``corrupt_pdf``; there is no
        flagged-publish path until a repaired-PDF review flow exists.

    Raises:
        PdfArtifactError: With a sanitized reason code on validation, size,
            filesystem or store failure. Never embeds PDF content or paths.
    """
    if not hasattr(store, "put_stream") or not hasattr(store, "root"):
        raise PdfArtifactError("invalid_store")
    checked_limits = _validate_limits(limits)
    source_path = _validate_path_arg(source, "invalid_path")

    inspected: list[ImportResult] = []

    def inspect_stage(stage_fd: int, sha256: str, size_bytes: int) -> None:
        try:
            head = os.pread(stage_fd, _HEAD_BYTES, 0)
        except OSError:
            raise PdfArtifactError("artifact_root_unavailable") from None
        inspection = _inspect_staged_fd(
            stage_fd, sha256=sha256, size_bytes=size_bytes, head=head, limits=checked_limits
        )
        if inspection.outcome == "reject":
            raise PdfArtifactError(inspection.reason or "corrupt_pdf", inspection=inspection)
        if inspection.page_count is None or inspection.page_count < 1:
            raise PdfArtifactError("corrupt_pdf", inspection=inspection)
        inspected.append(inspection)

    source_fd = _open_regular_nofollow(
        source_path, not_regular_code="input_not_regular", unavailable_code="input_unavailable"
    )
    try:
        stored = store.put_stream(source_fd, max_bytes=checked_limits.max_bytes, inspect=inspect_stage)
    except PdfArtifactError:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to fixed codes, details never leak
        raise PdfArtifactError(_store_error_code(exc)) from None
    finally:
        os.close(source_fd)

    inspection = inspected[-1]
    return IngestedPdfArtifact(
        outcome=inspection.outcome,
        reason=inspection.reason,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        page_count=inspection.page_count,
        object_key=stored.object_key,
        object_version=stored.object_version,
        artifact_path=Path(store.root) / stored.object_key,
        reused_existing=bool(getattr(stored, "reused_existing", False)),
        warnings=inspection.warnings,
    )


def _store_error_code(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "PublicationDurabilityUnknown":
        return "publication_durability_unknown"
    if name == "StreamTooLarge":
        return "file_too_large"
    if name == "StorageCorruption":
        return "artifact_corrupted"
    if isinstance(exc, OSError):
        return "artifact_root_unavailable"
    return "artifact_store_failed"
