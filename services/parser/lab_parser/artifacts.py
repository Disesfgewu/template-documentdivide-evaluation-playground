"""Bounded local original-PDF ingestion into the content-addressed artifact store.

Opens a regular local PDF without following symlinks and hands it to the
store's ``put_stream``, which stages, hashes and publishes it (the single
publisher for the ``sha256/<xx>/<sha256>`` layout; this module has no second
copy of staging, verification or publication). Before anything is published a
hook validates the staged bytes strictly with pypdf (size, ``%PDF-`` header,
structure, encryption, page limit, xref repair), so a rejected PDF never
reaches the store.

The ``store`` argument is any object with ``root`` and ``put_stream(fd,
max_bytes=..., inspect=...)``, in practice ``lab_db.storage.LocalArtifactStore``;
it is passed in so the parser does not import the database package.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import io
import logging
import os
from pathlib import Path
import stat
from typing import Any, Literal

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

from .importer import (
    ImportResult,
    Limits,
    _INSPECTION_LOCK,
    _WarningCollector,
)

__all__ = [
    "OBJECT_VERSION",
    "PdfArtifactError",
    "IngestedPdfArtifact",
    "ingest_pdf_artifact",
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

    outcome: Literal["accept", "flag"]
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


def _validate_limits(limits: Limits | None, allow_flagged: bool) -> Limits:
    if not isinstance(allow_flagged, bool):
        raise PdfArtifactError("invalid_options")
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


def _inspect_staged_fd(
    stage_fd: int,
    *,
    sha256: str,
    size_bytes: int,
    head: bytes,
    limits: Limits,
) -> ImportResult:
    if size_bytes > limits.max_bytes:
        return ImportResult(
            outcome="reject",
            reason="file_too_large",
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=None,
            warnings=(),
        )
    if size_bytes == 0:
        return ImportResult(
            outcome="reject",
            reason="empty_file",
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=None,
            warnings=(),
        )

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

    try:
        dup_fd = os.dup(stage_fd)
        os.lseek(dup_fd, 0, os.SEEK_SET)
        stream = io.FileIO(dup_fd, mode="rb", closefd=True)
        buffered = io.BufferedReader(stream)
    except OSError:
        raise PdfArtifactError("artifact_root_unavailable") from None

    with buffered:
        with _INSPECTION_LOCK:
            pypdf_logger = logging.getLogger("pypdf")
            collector = _WarningCollector()
            pypdf_logger.addHandler(collector)
            old_level = pypdf_logger.level
            if pypdf_logger.level == logging.NOTSET or pypdf_logger.level > logging.WARNING:
                pypdf_logger.setLevel(logging.WARNING)

            try:
                try:
                    reader = PdfReader(buffered, strict=True)
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
                    return ImportResult(
                        outcome="reject",
                        reason="encrypted_pdf",
                        sha256=sha256,
                        size_bytes=size_bytes,
                        page_count=None,
                        warnings=tuple(collector.warnings),
                    )
                except Exception:
                    return ImportResult(
                        outcome="reject",
                        reason="corrupt_pdf",
                        sha256=sha256,
                        size_bytes=size_bytes,
                        page_count=None,
                        warnings=tuple(collector.warnings),
                    )

                warnings_tuple = tuple(collector.warnings)
                if page_count < 1:
                    return ImportResult(
                        outcome="reject",
                        reason="corrupt_pdf",
                        sha256=sha256,
                        size_bytes=size_bytes,
                        page_count=None,
                        warnings=warnings_tuple,
                    )
                if page_count > limits.max_pages:
                    return ImportResult(
                        outcome="reject",
                        reason="page_limit_exceeded",
                        sha256=sha256,
                        size_bytes=size_bytes,
                        page_count=page_count,
                        warnings=warnings_tuple,
                    )

                if any("repaired" in w.lower() for w in warnings_tuple):
                    return ImportResult(
                        outcome="flag",
                        reason="xref_repaired",
                        sha256=sha256,
                        size_bytes=size_bytes,
                        page_count=page_count,
                        warnings=warnings_tuple,
                    )

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



def ingest_pdf_artifact(
    source: str | Path,
    store: Any,
    *,
    limits: Limits | None = None,
    allow_flagged: bool = False,
) -> IngestedPdfArtifact:
    """Stream, strictly validate, and publish a regular local PDF.

    Args:
        source: Path to the local regular PDF file (symlinks/FIFOs rejected).
        store: The artifact store (``LocalArtifactStore``) that publishes it.
        limits: Byte and page bounds (defaults to SPEC 16.6: 512 MiB, 2000 pages).
        allow_flagged: Whether ``xref_repaired`` flagged PDFs may be published.
            Defaults to False so repaired PDFs are not published without an
            explicit caller gate.

    Raises:
        PdfArtifactError: With a sanitized reason code on validation, size,
            filesystem or store failure. Never embeds PDF content or paths.
    """
    if not hasattr(store, "put_stream") or not hasattr(store, "root"):
        raise PdfArtifactError("invalid_store")
    checked_limits = _validate_limits(limits, allow_flagged)
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
        if inspection.outcome == "flag" and not allow_flagged:
            raise PdfArtifactError(inspection.reason or "xref_repaired", inspection=inspection)
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
    if name == "StreamTooLarge":
        return "file_too_large"
    if name == "StorageCorruption":
        return "artifact_corrupted"
    if isinstance(exc, OSError):
        return "artifact_root_unavailable"
    return "artifact_store_failed"
