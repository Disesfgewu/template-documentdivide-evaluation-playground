"""Local content-addressed artifact storage and DB registration workflow (SPEC 10.2)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

from .artifacts import ArtifactError, commit, stage

_OBJECT_KEY_RE = re.compile(r"\Asha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")


class StorageCorruption(ArtifactError):
    """Raised when stored artifact bytes do not match their content-addressed SHA-256."""


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    object_version: str
    sha256: str
    size_bytes: int


class LocalArtifactStore:
    """Content-addressed local filesystem store under an existing absolute root directory."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, (Path, str)):
            raise ValueError("root must be a Path or str")
        root_path = Path(root)
        if not root_path.is_absolute():
            raise ValueError(f"root must be an absolute path: {root!r}")
        if not root_path.exists() or not root_path.is_dir():
            raise ValueError(f"root must be an existing directory: {root!r}")
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

    def put(self, data: bytes) -> StoredObject:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        size_bytes = len(data)
        object_key = f"sha256/{digest[:2]}/{digest}"
        target, _ = self._resolve_key(object_key)

        if target.exists():
            if not target.is_file():
                raise StorageCorruption(
                    f"existing path for {object_key} is not a regular file"
                )
            actual_sha256, actual_size = self.verify(object_key)
            if actual_sha256 != digest or actual_size != size_bytes:
                raise StorageCorruption(
                    f"existing object {object_key} is corrupted: "
                    f"expected ({digest}, {size_bytes}), "
                    f"got ({actual_sha256}, {actual_size})"
                )
            return StoredObject(
                object_key=object_key,
                object_version="1",
                sha256=digest,
                size_bytes=size_bytes,
            )

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".tmp-{digest[:8]}-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(data)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return StoredObject(
            object_key=object_key,
            object_version="1",
            sha256=digest,
            size_bytes=size_bytes,
        )

    def open(self, object_key: str) -> bytes:
        target, expected_sha256 = self._resolve_key(object_key)
        raw = target.read_bytes()
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != expected_sha256:
            raise StorageCorruption(
                f"checksum mismatch for {object_key}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        return raw

    def verify(self, object_key: str) -> tuple[str, int]:
        target, _ = self._resolve_key(object_key)
        raw = target.read_bytes()
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        return actual_sha256, len(raw)


def store_and_register(
    conn,
    store: LocalArtifactStore,
    project_id: uuid.UUID,
    data: bytes,
    mime: str,
    retention: str = "restricted",
) -> uuid.UUID:
    """Write ``data`` to ``store``, stage in DB, verify on disk, and commit.

    Idempotent per project: the same bytes already committed in this
    project return the existing artifact row (content-addressed key)."""
    stored = store.put(data)
    existing = conn.execute(
        "SELECT id, state::text, sha256 FROM artifacts "
        "WHERE project_id = %s AND object_key = %s AND object_version = %s",
        (project_id, stored.object_key, stored.object_version),
    ).fetchone()
    if existing is not None:
        art_id, state, sha = existing
        if sha != stored.sha256:
            raise StorageCorruption(f"artifact {art_id} registered with a different sha256")
        if state == "committed":
            return art_id
        if state == "staged":
            observed_sha256, observed_size = store.verify(stored.object_key)
            commit(conn, art_id, observed_sha256, observed_size)
            return art_id
        raise ValueError(f"artifact {art_id} is {state}; register new bytes instead")
    artifact_id = stage(
        conn,
        project_id,
        stored.object_key,
        stored.object_version,
        stored.sha256,
        stored.size_bytes,
        mime,
        retention=retention,
    )
    observed_sha256, observed_size = store.verify(stored.object_key)
    commit(conn, artifact_id, observed_sha256, observed_size)
    return artifact_id
