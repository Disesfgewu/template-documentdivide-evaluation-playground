"""Local content-addressed artifact storage and DB registration workflow (SPEC 10.2).

Security guarantees:
- Descriptor-relative traversal with ``O_NOFOLLOW`` starting from filesystem root ``/``
  prevents symlink traversal across all path components (root, sha256, prefix, and object).
- Direct descriptor-relative temporary file creation (``dir_fd=prefix_fd``) and atomic link
  publication (``os.link`` with ``follow_symlinks=False``) prevent race-condition overwriting
  and ensure concurrent writers cannot clobber existing objects.
- Newly created directory entries are fsynced to their respective parent directory descriptors
  for crash durability.
- Canonical path and directory identity verification (device and inode) at operation
  completion guarantees that if intermediate directories are swapped or redirected during
  publication, the operation fails closed with ``StorageCorruption``.

Threat model and operational limits:
- The store relies on standard filesystem permissions (DAC/MAC) to protect the store root
  and its parent directories against unauthorized concurrent modification.
- Protection is guaranteed during and within store operations; however, this cannot protect
  against an attacker with write permissions who alters, deletes, or swaps directories *after*
  an operation has successfully completed and descriptors are closed. Deployments must enforce
  strict directory permissions (e.g. 0700/0750 owned by the service user) on the store root
  and its ancestors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable
import uuid

import psycopg

from .artifacts import ArtifactError, ChecksumMismatch, InvalidState, commit

_OBJECT_KEY_RE = re.compile(r"\Asha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")


class StorageCorruption(ArtifactError):
    """Raised when stored artifact bytes do not match their content-addressed SHA-256."""


class StreamTooLarge(ArtifactError):
    """The streamed source is longer than ``max_bytes`` (nothing is published)."""


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    object_version: str
    sha256: str
    size_bytes: int
    # True when ``put_stream`` found the identical, verified object already stored.
    # Not part of equality: two stores of the same bytes are the same object.
    reused_existing: bool = field(default=False, compare=False)


def _dir_id(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _bounded_file_hash(f, expected_size: int) -> tuple[str, int]:
    """Hash at most the expected length plus one byte, even if the file grows."""
    digest = hashlib.sha256()
    size = 0
    while size <= expected_size:
        chunk = f.read(min(65536, expected_size + 1 - size))
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _open_root_no_follow(root: Path) -> int:
    """Traverse every absolute root component descriptor-relative from / with O_NOFOLLOW.

    Fails closed if any path component (ancestor or target directory) is a symlink,
    does not exist, or is not a directory.
    """
    if not isinstance(root, (Path, str)):
        raise ValueError("root must be a Path or str")
    root_path = Path(root)
    if not root_path.is_absolute():
        raise ValueError(f"root must be an absolute path: {root!r}")
    parts = [p for p in root_path.parts if p and p != "/"]
    if not parts:
        return os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    curr_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            try:
                st = os.stat(part, dir_fd=curr_fd, follow_symlinks=False)
            except FileNotFoundError as e:
                raise ValueError(
                    f"root component {part!r} does not exist: {root!r}"
                ) from e
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"root component {part!r} is a symlink: {root!r}")
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(
                    f"root component {part!r} is not a directory: {root!r}"
                )
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=curr_fd,
                )
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(
                        f"root component {part!r} is a symlink or not a directory: {root!r}"
                    ) from e
                raise
            os.close(curr_fd)
            curr_fd = next_fd
        return curr_fd
    except BaseException:
        os.close(curr_fd)
        raise


def _open_dir_no_follow(parent_fd: int, name: str, desc: str) -> int:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode):
        raise StorageCorruption(f"{desc} component {name!r} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise StorageCorruption(f"{desc} component {name!r} is not a directory")
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StorageCorruption(
                f"{desc} component {name!r} is invalid or symlink"
            ) from e
        raise


def _ensure_dir_no_follow(parent_fd: int, name: str, desc: str) -> int:
    created = False
    try:
        os.mkdir(name, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    if created:
        os.fsync(parent_fd)
    return _open_dir_no_follow(parent_fd, name, desc)


class LocalArtifactStore:
    """Content-addressed local filesystem store under an existing absolute root directory."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, (Path, str)):
            raise ValueError("root must be a Path or str")
        root_path = Path(root)
        if not root_path.is_absolute():
            raise ValueError(f"root must be an absolute path: {root!r}")
        root_fd = _open_root_no_follow(root_path)
        os.close(root_fd)
        self.root: Path = root_path

    def _resolve_key(self, object_key: str) -> tuple[Path, str]:
        if not isinstance(object_key, str):
            raise ValueError("object_key must be a str")
        match = _OBJECT_KEY_RE.match(object_key)
        if match is None:
            raise ValueError(f"invalid object_key format: {object_key!r}")
        prefix, digest = match.group(1), match.group(2)
        if prefix != digest[:2]:
            raise ValueError(
                f"invalid object_key prefix {prefix!r} for digest {digest!r}"
            )
        target = self.root / "sha256" / prefix / digest
        return target, digest

    def _read_bytes(
        self,
        object_key: str,
        prefix: str,
        digest: str,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        if expected_size is not None and (not isinstance(expected_size, int) or expected_size < 0):
            raise ValueError("expected_size must be a non-negative integer")
        if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 0):
            raise ValueError("max_bytes must be a non-negative integer")
        if expected_size is not None and max_bytes is not None and expected_size > max_bytes:
            raise StorageCorruption(
                f"object {object_key} expected_size {expected_size} exceeds limit {max_bytes}"
            )
        try:
            root_fd = _open_root_no_follow(self.root)
        except ValueError as e:
            raise StorageCorruption(
                f"root directory traversal failed or contains symlink: {e}"
            ) from e
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageCorruption(
                    f"root directory is invalid or symlink: {self.root!r}"
                ) from e
            raise
        try:
            sha256_fd = _open_dir_no_follow(root_fd, "sha256", "sha256")
        finally:
            os.close(root_fd)

        try:
            prefix_fd = _open_dir_no_follow(sha256_fd, prefix, "prefix")
        finally:
            os.close(sha256_fd)

        try:
            try:
                st = os.stat(digest, dir_fd=prefix_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise
            if stat.S_ISLNK(st.st_mode):
                raise StorageCorruption(f"object {object_key} is a symlink")
            if not stat.S_ISREG(st.st_mode):
                raise StorageCorruption(f"object {object_key} is not a regular file")
            try:
                file_fd = os.open(
                    digest,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=prefix_fd,
                )
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StorageCorruption(
                        f"object {object_key} is invalid or symlink"
                    ) from e
                raise
            try:
                with os.fdopen(file_fd, "rb") as f:
                    fst = os.fstat(f.fileno())
                    if not stat.S_ISREG(fst.st_mode):
                        raise StorageCorruption(
                            f"object {object_key} is not a regular file"
                        )
                    if expected_size is not None:
                        if fst.st_size != expected_size:
                            raise StorageCorruption(
                                f"object {object_key} size mismatch: "
                                f"expected {expected_size}, got {fst.st_size}"
                            )
                        raw = f.read(expected_size)
                        if len(raw) != expected_size:
                            raise StorageCorruption(
                                f"object {object_key} truncated: "
                                f"expected {expected_size}, got {len(raw)}"
                            )
                        if f.read(1):
                            raise StorageCorruption(
                                f"object {object_key} grew concurrently beyond expected size {expected_size}"
                            )
                        return raw
                    if max_bytes is not None:
                        if fst.st_size > max_bytes:
                            raise StorageCorruption(
                                f"object {object_key} size {fst.st_size} exceeds limit {max_bytes}"
                            )
                        raw = f.read(max_bytes)
                        if f.read(1):
                            raise StorageCorruption(
                                f"object {object_key} grew concurrently beyond limit {max_bytes}"
                            )
                        return raw
                    return f.read()
            except BaseException:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(prefix_fd)

    def _verify_existing_in_prefix(
        self, prefix_fd: int, object_key: str, digest: str, expected_size: int
    ) -> None:
        try:
            st = os.stat(digest, dir_fd=prefix_fd, follow_symlinks=False)
        except OSError as e:
            raise StorageCorruption(f"failed to stat existing object {object_key}") from e
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise StorageCorruption(
                f"existing path for {object_key} is not a regular file"
            )
        try:
            file_fd = os.open(
                digest,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=prefix_fd,
            )
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageCorruption(
                    f"existing path for {object_key} is not a regular file"
                ) from e
            raise StorageCorruption(f"failed to open existing object {object_key}") from e
        try:
            with os.fdopen(file_fd, "rb") as f:
                fst = os.fstat(f.fileno())
                if not stat.S_ISREG(fst.st_mode):
                    raise StorageCorruption(
                        f"existing path for {object_key} is not a regular file"
                    )
                actual_sha256, actual_size = _bounded_file_hash(f, expected_size)
        except BaseException:
            try:
                os.close(file_fd)
            except OSError:
                pass
            raise

        if actual_sha256 != digest or actual_size != expected_size:
            raise StorageCorruption(
                f"existing object {object_key} is corrupted: "
                f"expected ({digest}, {expected_size}), "
                f"got ({actual_sha256}, {actual_size})"
            )

    def _verify_canonical_completion(
        self,
        object_key: str,
        prefix: str,
        digest: str,
        expected_size: int,
        expected_root_id: tuple[int, int],
        expected_sha256_id: tuple[int, int],
        expected_prefix_id: tuple[int, int],
    ) -> None:
        try:
            canonical_root_fd = _open_root_no_follow(self.root)
        except ValueError as e:
            raise StorageCorruption(
                f"canonical root traversal failed or contains symlink: {e}"
            ) from e
        except OSError as e:
            raise StorageCorruption(
                f"canonical root access failed: {e}"
            ) from e
        try:
            if _dir_id(canonical_root_fd) != expected_root_id:
                raise StorageCorruption(
                    f"canonical root directory identity changed during publication of {object_key}"
                )
            try:
                canonical_sha256_fd = _open_dir_no_follow(
                    canonical_root_fd, "sha256", "sha256"
                )
            except (StorageCorruption, OSError) as e:
                raise StorageCorruption(
                    f"canonical sha256 directory access failed during publication: {e}"
                ) from e
        finally:
            os.close(canonical_root_fd)

        try:
            if _dir_id(canonical_sha256_fd) != expected_sha256_id:
                raise StorageCorruption(
                    f"canonical sha256 directory identity changed during publication of {object_key}"
                )
            try:
                canonical_prefix_fd = _open_dir_no_follow(
                    canonical_sha256_fd, prefix, "prefix"
                )
            except (StorageCorruption, OSError) as e:
                raise StorageCorruption(
                    f"canonical prefix directory access failed during publication: {e}"
                ) from e
        finally:
            os.close(canonical_sha256_fd)

        try:
            if _dir_id(canonical_prefix_fd) != expected_prefix_id:
                raise StorageCorruption(
                    f"canonical prefix directory identity changed during publication of {object_key}"
                )
            try:
                st = os.stat(digest, dir_fd=canonical_prefix_fd, follow_symlinks=False)
            except OSError as e:
                raise StorageCorruption(
                    f"canonical stat for {object_key} failed: {e}"
                ) from e
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise StorageCorruption(
                    f"canonical path for {object_key} is invalid or symlink"
                )
            try:
                file_fd = os.open(
                    digest,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=canonical_prefix_fd,
                )
            except OSError as e:
                raise StorageCorruption(
                    f"canonical open for {object_key} failed: {e}"
                ) from e
            try:
                with os.fdopen(file_fd, "rb") as f:
                    fst = os.fstat(f.fileno())
                    if not stat.S_ISREG(fst.st_mode):
                        raise StorageCorruption(
                            f"canonical path for {object_key} is not a regular file"
                        )
                    actual_sha256, actual_size = _bounded_file_hash(f, expected_size)
            except BaseException:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(canonical_prefix_fd)

        if actual_sha256 != digest or actual_size != expected_size:
            raise StorageCorruption(
                f"canonical content verification failed for {object_key}: "
                f"expected ({digest}, {expected_size}), "
                f"got ({actual_sha256}, {actual_size})"
            )

    def _open_root_checked(self) -> int:
        """Open the store root without following symlinks; failures are StorageCorruption."""
        try:
            return _open_root_no_follow(self.root)
        except ValueError as e:
            raise StorageCorruption(
                f"root directory traversal failed or contains symlink: {e}"
            ) from e
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageCorruption(
                    f"root directory is invalid or symlink: {self.root!r}"
                ) from e
            raise

    def put(self, data: bytes) -> StoredObject:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        size_bytes = len(data)
        object_key = f"sha256/{digest[:2]}/{digest}"
        prefix = digest[:2]
        target, _ = self._resolve_key(object_key)

        root_fd = self._open_root_checked()
        root_id = _dir_id(root_fd)
        try:
            sha256_fd = _ensure_dir_no_follow(root_fd, "sha256", "sha256")
            sha256_id = _dir_id(sha256_fd)
        finally:
            os.close(root_fd)

        try:
            prefix_fd = _ensure_dir_no_follow(sha256_fd, prefix, "prefix")
            prefix_id = _dir_id(prefix_fd)
        finally:
            os.close(sha256_fd)

        try:
            try:
                st = os.stat(digest, dir_fd=prefix_fd, follow_symlinks=False)
            except FileNotFoundError:
                st = None

            if st is not None:
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    raise StorageCorruption(
                        f"existing path for {object_key} is not a regular file"
                    )
                self._verify_existing_in_prefix(
                    prefix_fd, object_key, digest, size_bytes
                )
                self._verify_canonical_completion(
                    object_key,
                    prefix,
                    digest,
                    size_bytes,
                    root_id,
                    sha256_id,
                    prefix_id,
                )
                return StoredObject(
                    object_key=object_key,
                    object_version="1",
                    sha256=digest,
                    size_bytes=size_bytes,
                )

            # Create random temporary file descriptor-relative to prefix_fd
            tmp_name = None
            fd = None
            for _ in range(128):
                candidate = f".tmp-{digest[:8]}-{uuid.uuid4().hex}"
                try:
                    fd = os.open(
                        candidate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        mode=0o600,
                        dir_fd=prefix_fd,
                    )
                    tmp_name = candidate
                    break
                except FileExistsError:
                    continue
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise StorageCorruption(
                            "prefix directory descriptor is invalid or symlink"
                        ) from e
                    raise

            if fd is None or tmp_name is None:
                raise StorageCorruption("failed to create temporary file in store")

            try:
                with os.fdopen(fd, "wb") as tmp_file:
                    tmp_file.write(data)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())

                try:
                    os.link(
                        tmp_name,
                        digest,
                        src_dir_fd=prefix_fd,
                        dst_dir_fd=prefix_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    try:
                        os.unlink(tmp_name, dir_fd=prefix_fd)
                    except OSError:
                        pass
                    self._verify_existing_in_prefix(
                        prefix_fd, object_key, digest, size_bytes
                    )
                    self._verify_canonical_completion(
                        object_key,
                        prefix,
                        digest,
                        size_bytes,
                        root_id,
                        sha256_id,
                        prefix_id,
                    )
                    return StoredObject(
                        object_key=object_key,
                        object_version="1",
                        sha256=digest,
                        size_bytes=size_bytes,
                    )
                else:
                    try:
                        os.unlink(tmp_name, dir_fd=prefix_fd)
                    except OSError:
                        pass
                    os.fsync(prefix_fd)
                    self._verify_canonical_completion(
                        object_key,
                        prefix,
                        digest,
                        size_bytes,
                        root_id,
                        sha256_id,
                        prefix_id,
                    )
                    return StoredObject(
                        object_key=object_key,
                        object_version="1",
                        sha256=digest,
                        size_bytes=size_bytes,
                    )
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=prefix_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(prefix_fd)

    def put_stream(
        self,
        source_fd: int,
        *,
        max_bytes: int,
        inspect: Callable[[int, str, int], None] | None = None,
    ) -> StoredObject:
        """Publish the rest of an open file without holding it in memory.

        The caller opens the source (``O_NOFOLLOW``, regular file) and owns the
        descriptor. At most ``max_bytes + 1`` bytes are read; a longer source
        raises ``StreamTooLarge`` and nothing is published. The bytes are
        staged privately (0600) under ``sha256/``, hashed as they are written
        and fsynced. ``inspect(stage_fd, sha256, size_bytes)`` may reject the
        content by raising; it runs before anything is published and may read
        the staged descriptor. Immediately before publication the staged
        descriptor and its directory entry are checked again (same inode, one
        link, mode, size and SHA-256), so a swapped or rewritten stage file
        fails closed with ``StorageCorruption``. Publication, the check of an
        already stored identical object and the final canonical-path check are
        the same as ``put``. The stage file is removed on every path; a stage
        file left by a crash is never published.
        """
        if not isinstance(source_fd, int) or isinstance(source_fd, bool) or source_fd < 0:
            raise ValueError("source_fd must be an open file descriptor")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")

        root_fd = self._open_root_checked()
        root_id = _dir_id(root_fd)
        try:
            sha256_fd = _ensure_dir_no_follow(root_fd, "sha256", "sha256")
            sha256_id = _dir_id(sha256_fd)
        finally:
            os.close(root_fd)

        stage_name = None
        stage_fd = -1
        prefix_fd = -1
        try:
            for _ in range(128):
                candidate = f".stage-{uuid.uuid4().hex}"
                try:
                    stage_fd = os.open(
                        candidate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        mode=0o600,
                        dir_fd=sha256_fd,
                    )
                    stage_name = candidate
                    break
                except FileExistsError:
                    continue
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise StorageCorruption("sha256 directory descriptor is invalid or symlink") from e
                    raise
            if stage_name is None:
                raise StorageCorruption("failed to create staging file in store")

            hasher = hashlib.sha256()
            size_bytes = 0
            while size_bytes <= max_bytes:
                chunk = os.read(source_fd, min(65536, max_bytes + 1 - size_bytes))
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(stage_fd, view):]
                hasher.update(chunk)
                size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise StreamTooLarge(f"source is longer than {max_bytes} bytes")
            os.fsync(stage_fd)
            digest = hasher.hexdigest()

            if inspect is not None:
                inspect(stage_fd, digest, size_bytes)

            self._verify_stage(sha256_fd, stage_name, stage_fd, digest, size_bytes)

            object_key = f"sha256/{digest[:2]}/{digest}"
            prefix = digest[:2]
            self._resolve_key(object_key)
            prefix_fd = _ensure_dir_no_follow(sha256_fd, prefix, "prefix")
            prefix_id = _dir_id(prefix_fd)

            reused = False
            try:
                os.link(stage_name, digest, src_dir_fd=sha256_fd, dst_dir_fd=prefix_fd, follow_symlinks=False)
            except FileExistsError:
                reused = True
            if reused:
                self._verify_existing_in_prefix(prefix_fd, object_key, digest, size_bytes)
            else:
                os.fsync(prefix_fd)
            self._verify_canonical_completion(
                object_key, prefix, digest, size_bytes, root_id, sha256_id, prefix_id
            )
            return StoredObject(
                object_key=object_key,
                object_version="1",
                sha256=digest,
                size_bytes=size_bytes,
                reused_existing=reused,
            )
        finally:
            try:
                if stage_name is not None:
                    try:
                        os.unlink(stage_name, dir_fd=sha256_fd)
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        raise StorageCorruption("failed to remove staging file") from e
            finally:
                for fd in (stage_fd, prefix_fd, sha256_fd):
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    @staticmethod
    def _verify_stage(sha256_fd: int, stage_name: str, stage_fd: int, digest: str, size_bytes: int) -> None:
        try:
            fst = os.fstat(stage_fd)
            dst = os.stat(stage_name, dir_fd=sha256_fd, follow_symlinks=False)
        except OSError as e:
            raise StorageCorruption("staging file is missing or unreadable before publication") from e
        if (
            (fst.st_dev, fst.st_ino) != (dst.st_dev, dst.st_ino)
            or not stat.S_ISREG(dst.st_mode)
            or dst.st_nlink != 1
            or stat.S_IMODE(dst.st_mode) != 0o600
            or dst.st_size != size_bytes
        ):
            raise StorageCorruption("staging file was replaced or modified before publication")
        try:
            reader = os.dup(stage_fd)
            os.lseek(reader, 0, os.SEEK_SET)
            with os.fdopen(reader, "rb") as f:
                actual, actual_size = _bounded_file_hash(f, size_bytes)
        except OSError as e:
            raise StorageCorruption("staging file is unreadable before publication") from e
        if actual != digest or actual_size != size_bytes:
            raise StorageCorruption("staging file content changed before publication")

    def open(
        self,
        object_key: str,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        target, expected_sha256 = self._resolve_key(object_key)
        prefix = expected_sha256[:2]
        raw = self._read_bytes(
            object_key,
            prefix,
            expected_sha256,
            expected_size=expected_size,
            max_bytes=max_bytes,
        )
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != expected_sha256:
            raise StorageCorruption(
                f"checksum mismatch for {object_key}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        if expected_size is not None and len(raw) != expected_size:
            raise StorageCorruption(
                f"size mismatch for {object_key}: "
                f"expected {expected_size}, got {len(raw)}"
            )
        return raw

    def verify(
        self,
        object_key: str,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[str, int]:
        target, expected_sha256 = self._resolve_key(object_key)
        prefix = expected_sha256[:2]
        raw = self._read_bytes(
            object_key,
            prefix,
            expected_sha256,
            expected_size=expected_size,
            max_bytes=max_bytes,
        )
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        return actual_sha256, len(raw)


def store_and_register(
    conn: psycopg.Connection,
    store: LocalArtifactStore,
    project_id: uuid.UUID,
    data: bytes,
    mime: str,
    retention: str = "restricted",
) -> uuid.UUID:
    """Write ``data`` to ``store``, register it as staged, verify on disk, commit.

    Two phases: the staged row is registered (race-free upsert) in its own
    transaction, then verified and committed under a row lock. A checksum
    mismatch therefore leaves the staged row in place for grace-period GC
    instead of an untracked file. Idempotent per project when ``mime`` and
    ``retention`` match; a mismatch raises instead of silently downgrading.
    """
    stored = store.put(data)
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO artifacts (id, project_id, object_key, object_version,
                                   sha256, size_bytes, mime, state, retention)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'staged', %s)
            ON CONFLICT (project_id, object_key, object_version) DO NOTHING
            """,
            (uuid.uuid4(), project_id, stored.object_key, stored.object_version,
             stored.sha256, stored.size_bytes, mime, retention),
        )
    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, state::text, sha256, mime, retention FROM artifacts
            WHERE project_id = %s AND object_key = %s AND object_version = %s
            FOR UPDATE
            """,
            (project_id, stored.object_key, stored.object_version),
        ).fetchone()
        if row is None:
            raise StorageCorruption(
                f"artifact row for ({project_id}, {stored.object_key}) disappeared after registration")
        art_id, state, sha, existing_mime, existing_retention = row
        if sha != stored.sha256:
            raise StorageCorruption(f"artifact {art_id} registered with a different sha256")
        if existing_mime != mime:
            raise ValueError(f"artifact {art_id} mime mismatch: existing {existing_mime!r} != requested {mime!r}")
        if existing_retention != retention:
            raise ValueError(
                f"artifact {art_id} retention mismatch: existing {existing_retention!r} != requested {retention!r}")
        if state == "committed":
            return art_id
        if state != "staged":
            raise ValueError(f"artifact {art_id} is {state}; register new bytes instead")
    # Verify outside the lock-holding transaction's error path: a mismatch
    # must not roll back the staged registration above.
    try:
        observed_sha256, observed_size = store.verify(
            stored.object_key, expected_size=stored.size_bytes
        )
    except StorageCorruption as exc:
        raise ChecksumMismatch(f"Artifact {stored.object_key} verification failed: {exc}") from exc
    try:
        commit(conn, art_id, observed_sha256, observed_size)
    except InvalidState:
        # A concurrent caller committed the same row first: that is success.
        state = conn.execute("SELECT state::text FROM artifacts WHERE id = %s", (art_id,)).fetchone()[0]
        if state != "committed":
            raise
    return art_id
