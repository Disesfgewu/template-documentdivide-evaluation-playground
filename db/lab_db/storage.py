"""Local content-addressed artifact storage and DB registration workflow (SPEC 10.2)."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

import psycopg

from .artifacts import ArtifactError, InvalidState, commit

_OBJECT_KEY_RE = re.compile(r"\Asha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")


class StorageCorruption(ArtifactError):
    """Raised when stored artifact bytes do not match their content-addressed SHA-256."""


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    object_version: str
    sha256: str
    size_bytes: int


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
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
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

    def _read_bytes(self, object_key: str, prefix: str, digest: str) -> bytes:
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
                raw = f.read()
        except BaseException:
            try:
                os.close(file_fd)
            except OSError:
                pass
            raise

        actual_sha256 = hashlib.sha256(raw).hexdigest()
        actual_size = len(raw)
        if actual_sha256 != digest or actual_size != expected_size:
            raise StorageCorruption(
                f"existing object {object_key} is corrupted: "
                f"expected ({digest}, {expected_size}), "
                f"got ({actual_sha256}, {actual_size})"
            )

    def put(self, data: bytes) -> StoredObject:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        size_bytes = len(data)
        object_key = f"sha256/{digest[:2]}/{digest}"
        prefix = digest[:2]
        target, _ = self._resolve_key(object_key)

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
            sha256_fd = _ensure_dir_no_follow(root_fd, "sha256", "sha256")
        finally:
            os.close(root_fd)

        try:
            prefix_fd = _ensure_dir_no_follow(sha256_fd, prefix, "prefix")
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
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=prefix_fd)
                except OSError:
                    pass
                raise

            return StoredObject(
                object_key=object_key,
                object_version="1",
                sha256=digest,
                size_bytes=size_bytes,
            )
        finally:
            os.close(prefix_fd)

    def open(self, object_key: str) -> bytes:
        target, expected_sha256 = self._resolve_key(object_key)
        prefix = expected_sha256[:2]
        raw = self._read_bytes(object_key, prefix, expected_sha256)
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != expected_sha256:
            raise StorageCorruption(
                f"checksum mismatch for {object_key}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        return raw

    def verify(self, object_key: str) -> tuple[str, int]:
        target, expected_sha256 = self._resolve_key(object_key)
        prefix = expected_sha256[:2]
        raw = self._read_bytes(object_key, prefix, expected_sha256)
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
    observed_sha256, observed_size = store.verify(stored.object_key)
    try:
        commit(conn, art_id, observed_sha256, observed_size)
    except InvalidState:
        # A concurrent caller committed the same row first: that is success.
        state = conn.execute("SELECT state::text FROM artifacts WHERE id = %s", (art_id,)).fetchone()[0]
        if state != "committed":
            raise
    return art_id
