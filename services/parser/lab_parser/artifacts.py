"""Bounded local original-PDF artifact ingestion into content-addressed storage.

Streams a regular local PDF into a private stage file under an existing
artifact root, computes SHA-256 while bounding read bytes, validates the staged
PDF strictly with ``inspect_pdf``, and atomically publishes immutable original
bytes to ``sha256/<prefix>/<sha256>`` without clobbering existing destinations.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import io
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Literal

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
    "STAGE_PREFIX",
    "PdfArtifactError",
    "IngestedPdfArtifact",
    "ingest_pdf_artifact",
]

OBJECT_VERSION = "1"
STAGE_PREFIX = ".stage-pdf-"
_CHUNK_BYTES = 64 * 1024


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


def _open_root_dir(artifact_root: Path) -> int:
    if not artifact_root.is_absolute():
        raise PdfArtifactError("invalid_artifact_root")
    try:
        fd = os.open(
            artifact_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        st = os.fstat(fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise PdfArtifactError("invalid_artifact_root") from None
        raise PdfArtifactError("artifact_root_unavailable") from None

    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise PdfArtifactError("invalid_artifact_root")
    return fd


def _ensure_subdir_nofollow(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError:
        raise PdfArtifactError("artifact_root_unavailable") from None

    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        st = os.fstat(fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise PdfArtifactError("artifact_corrupted") from None
        raise PdfArtifactError("artifact_root_unavailable") from None

    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise PdfArtifactError("artifact_corrupted")
    return fd


def _fsync_dir_fd(dir_fd: int, error_code: str = "artifact_root_unavailable") -> None:
    try:
        os.fsync(dir_fd)
    except OSError:
        raise PdfArtifactError(error_code) from None


def _stream_source_to_stage(
    source_fd: int,
    stage_fd: int,
    max_bytes: int,
) -> tuple[str, int, bytes]:
    hasher = hashlib.sha256()
    size_bytes = 0
    head = bytearray()
    try:
        while True:
            remaining = (max_bytes + 1) - size_bytes
            chunk = os.read(source_fd, min(_CHUNK_BYTES, remaining))
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise PdfArtifactError("file_too_large")
            hasher.update(chunk)
            if len(head) < 1028:
                need = 1028 - len(head)
                head.extend(chunk[:need])
            view = memoryview(chunk)
            written = 0
            while written < len(chunk):
                n = os.write(stage_fd, view[written:])
                if n <= 0:
                    raise PdfArtifactError("artifact_root_unavailable")
                written += n
        os.fsync(stage_fd)
    except PdfArtifactError:
        raise
    except OSError:
        raise PdfArtifactError("input_unavailable") from None

    return hasher.hexdigest(), size_bytes, bytes(head)


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


def _verify_fd_digest(
    fd: int,
    *,
    expected_sha256: str,
    expected_size: int,
    corruption_code: str,
) -> None:
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
            raise PdfArtifactError(corruption_code)
        os.lseek(fd, 0, os.SEEK_SET)
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise PdfArtifactError(corruption_code)
            hasher.update(chunk)
    except PdfArtifactError:
        raise
    except OSError:
        raise PdfArtifactError(corruption_code) from None

    if total != expected_size or hasher.hexdigest() != expected_sha256:
        raise PdfArtifactError(corruption_code)


def _verify_existing_target(
    dir_fd: int,
    name: str,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
        _verify_fd_digest(
            fd,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            corruption_code="artifact_corrupted",
        )
    except PdfArtifactError:
        raise
    except OSError:
        raise PdfArtifactError("artifact_corrupted") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _rename_noreplace(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
) -> bool:
    """Atomically link/rename stage to destination without clobbering.

    Returns True when newly published, False if destination already existed.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise PdfArtifactError("publication_unsupported")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    rc = rename(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        1,  # RENAME_NOREPLACE
    )
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err in (errno.EEXIST, errno.ENOTEMPTY):
        return False
    if err in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise PdfArtifactError("publication_unsupported")
    raise PdfArtifactError("artifact_root_unavailable")


def ingest_pdf_artifact(
    source: str | Path,
    artifact_root: str | Path,
    *,
    limits: Limits | None = None,
    allow_flagged: bool = False,
) -> IngestedPdfArtifact:
    """Stream, strictly validate, and publish a regular local PDF artifact.

    Args:
        source: Path to the local regular PDF file (symlinks/FIFOs rejected).
        artifact_root: Existing absolute directory where ``sha256/<xx>/<sha256>``
            is published.
        limits: Byte and page bounds (defaults to SPEC 16.6: 512 MiB, 2000 pages).
        allow_flagged: Whether ``xref_repaired`` flagged PDFs may be published.
            Defaults to False so repaired PDFs are not published without an
            explicit caller gate.

    Returns:
        IngestedPdfArtifact describing the published or verified existing object.

    Raises:
        PdfArtifactError: With a sanitized reason code on validation, tampering,
            filesystem, or cleanup failure.
    """
    if sys.platform != "linux":
        raise PdfArtifactError("platform_unsupported")

    checked_limits = _validate_limits(limits, allow_flagged)
    source_path = _validate_path_arg(source, "invalid_path")
    root_path = _validate_path_arg(artifact_root, "invalid_artifact_root")

    root_fd = _open_root_dir(root_path)
    source_fd = -1
    stage_fd = -1
    stage_path: Path | None = None
    sha_dir_fd = -1
    prefix_dir_fd = -1

    try:
        source_fd = _open_regular_nofollow(
            source_path,
            not_regular_code="input_not_regular",
            unavailable_code="input_unavailable",
        )

        try:
            stage_fd, stage_name = tempfile.mkstemp(
                prefix=STAGE_PREFIX,
                dir=root_path,
            )
            os.fchmod(stage_fd, 0o600)
            stage_path = Path(stage_name)
        except OSError:
            raise PdfArtifactError("artifact_root_unavailable") from None

        sha256, size_bytes, head = _stream_source_to_stage(
            source_fd,
            stage_fd,
            checked_limits.max_bytes,
        )
        os.close(source_fd)
        source_fd = -1

        inspection = _inspect_staged_fd(
            stage_fd,
            sha256=sha256,
            size_bytes=size_bytes,
            head=head,
            limits=checked_limits,
        )

        if inspection.outcome == "reject":
            raise PdfArtifactError(
                inspection.reason or "corrupt_pdf",
                inspection=inspection,
            )
        if inspection.outcome == "flag" and not allow_flagged:
            raise PdfArtifactError(
                inspection.reason or "xref_repaired",
                inspection=inspection,
            )
        if inspection.page_count is None or inspection.page_count < 1:
            raise PdfArtifactError("corrupt_pdf", inspection=inspection)

        prefix = sha256[:2]
        object_key = f"sha256/{prefix}/{sha256}"
        artifact_path = root_path / "sha256" / prefix / sha256

        # Re-verify the staged file descriptor before publication so concurrent
        # modification of the staged bytes cannot publish unverified content.
        stage_stat = os.fstat(stage_fd)
        _verify_fd_digest(
            stage_fd,
            expected_sha256=sha256,
            expected_size=size_bytes,
            corruption_code="stage_tampered",
        )
        os.close(stage_fd)
        stage_fd = -1

        # Verify the directory entry in root_fd still points to the exact same
        # regular file inode and matches the digest right before publication.
        reopen_fd = _open_regular_nofollow(
            stage_path.name,
            not_regular_code="stage_tampered",
            unavailable_code="stage_tampered",
            dir_fd=root_fd,
        )
        try:
            reopen_stat = os.fstat(reopen_fd)
            if (
                (reopen_stat.st_dev, reopen_stat.st_ino)
                != (stage_stat.st_dev, stage_stat.st_ino)
                or reopen_stat.st_nlink != 1
                or stat.S_IMODE(reopen_stat.st_mode) != 0o600
            ):
                raise PdfArtifactError("stage_tampered")
            _verify_fd_digest(
                reopen_fd,
                expected_sha256=sha256,
                expected_size=size_bytes,
                corruption_code="stage_tampered",
            )
        finally:
            os.close(reopen_fd)

        sha_dir_fd = _ensure_subdir_nofollow(root_fd, "sha256")
        prefix_dir_fd = _ensure_subdir_nofollow(sha_dir_fd, prefix)

        published = _rename_noreplace(
            root_fd,
            stage_path.name,
            prefix_dir_fd,
            sha256,
        )
        if published:
            stage_path = None
            _fsync_dir_fd(prefix_dir_fd, "publication_durability_unknown")
            _fsync_dir_fd(sha_dir_fd, "publication_durability_unknown")
            _fsync_dir_fd(root_fd, "publication_durability_unknown")
            reused_existing = False
        else:
            # Destination already exists (e.g. duplicate or concurrent writer).
            # Verify existing content matches the expected SHA-256 and size.
            _verify_existing_target(
                prefix_dir_fd,
                sha256,
                expected_sha256=sha256,
                expected_size=size_bytes,
            )
            reused_existing = True

        return IngestedPdfArtifact(
            outcome=inspection.outcome,
            reason=inspection.reason,
            sha256=sha256,
            size_bytes=size_bytes,
            page_count=inspection.page_count,
            object_key=object_key,
            object_version=OBJECT_VERSION,
            artifact_path=artifact_path,
            reused_existing=reused_existing,
            warnings=inspection.warnings,
        )
    finally:
        try:
            if stage_path is not None:
                try:
                    os.unlink(
                        stage_path.name if root_fd >= 0 else stage_path,
                        dir_fd=root_fd if root_fd >= 0 else None,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    raise PdfArtifactError("cleanup_failed") from None
        finally:
            for fd in (prefix_dir_fd, sha_dir_fd, stage_fd, source_fd, root_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
