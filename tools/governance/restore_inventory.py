"""Read-only verification of a supplied committed artifact inventory against restored local bytes (Refs #14).

This module verifies that every committed artifact listed in an inventory
exists in the restored store at ``<store_root>/sha256/<prefix>/<digest>``,
is a regular file with exact matching size and content hash, and does not traverse
any symbolic links in any path component.

Security & Integrity Guarantees:
- Strictly read-only: no mutations, file creations, renames, unlinks, or deletions.
- Descriptor-relative traversal with ``O_NOFOLLOW`` starting from root ``/``
  ensures that neither the store root, its ancestors, nor intermediate directories
  (``sha256``, prefix, or object file) can traverse symbolic links.
- Non-regular files (FIFOs, character/block devices, directories, sockets) fail closed.
  Files are opened with ``O_NONBLOCK`` to prevent hanging on adversarial FIFOs.
- Bounded input: enforced limits on inventory payload size, item count, per-file bytes,
  and aggregate verified bytes. Bounded iteration guarantees memory safety against
  adversarial infinite generator streams.
- Pre-read aggregate bounds: total expected bytes are validated before opening or reading files.
- Streaming hash calculation: chunked 64 KiB reads avoid loading entire objects into memory.
- Canonical path and identity verification: re-verifies store root, sha256 directory,
  prefix directory, and object file canonical identity (device and inode) and reachability
  to detect directory swaps or rename races during verification.
- Inventory validation: enforces strict schema, duplicate field/key rejection,
  rejection of contradictory aliases (object_key vs key, size_bytes vs size),
  and verifies that any explicitly specified artifact state is strictly 'committed'
  (null/None state is rejected).

Threat Model and Post-Operation Boundary:
- The verification primitive guarantees that at the moment of verification, all traversed
  ancestors, store root, sha256, prefix directories, and object files exist canonically,
  match expected directory/file identities (dev and ino), and are non-symlink regular files
  matching the supplied committed inventory.
- However, this is an in-process read-only verification primitive. Once verification concludes
  and descriptors are closed, this tool cannot prevent a concurrent or subsequent local actor
  with write permissions from tampering with, replacing, renaming, or deleting files in the store.
- Deployments MUST enforce standard operating system access controls (DAC/MAC, such as permissions
  0700/0750 owned exclusively by the dedicated service user) on the store root and all ancestor
  directories to prevent post-verification modification.

Operational Scope and Limitations:
- The restored store must be quiescent and protected from all writers during
  verification and until the result is used. Checks are sequential, not an
  atomic snapshot; identity checks cannot prevent same-inode content changes.
- This tool verifies restored filesystem bytes against a supplied inventory.
- It does NOT claim that the supplied inventory is a consistent database snapshot.
  The operator must obtain and authenticate the inventory from a consistent database snapshot.
- It does NOT coordinate database and artifact restore, nor does it enforce project
  authorization or multi-tenant isolation.
- It does not represent completion of full T79, T80, or issue #14 acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Mapping

DEFAULT_MAX_INVENTORY_BYTES = 32 * 1024 * 1024      # 32 MiB max inventory file
DEFAULT_MAX_INVENTORY_ITEMS = 10_000                # 10,000 artifacts per batch
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024          # 512 MiB max per artifact file
DEFAULT_MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024   # 10 GiB aggregate verified bytes
READ_CHUNK_SIZE = 64 * 1024                         # 64 KiB read buffer

_OBJECT_KEY_RE = re.compile(r"\Asha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")
_ALLOWED_ITEM_FIELDS = {
    "object_key",
    "key",
    "sha256",
    "size_bytes",
    "size",
    "state",
    "object_version",
    "id",
    "project_id",
    "mime",
    "retention",
    "created_at",
    "committed_at",
}


class InventoryVerificationError(ValueError):
    """Raised when inventory verification fails or untrusted inputs are malformed."""


@dataclass(frozen=True)
class InventoryItem:
    """Normalized committed artifact inventory record."""

    object_key: str
    sha256: str
    size_bytes: int
    state: str = "committed"
    object_version: str = "1"


@dataclass(frozen=True)
class VerificationReport:
    """Summary of successful restored artifact inventory verification."""

    verified_count: int
    total_bytes: int
    store_root: str
    verified_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "verified",
            "verified_count": self.verified_count,
            "total_bytes": self.total_bytes,
            "store_root": self.store_root,
            "verified_keys": list(self.verified_keys),
        }


def _dir_id(fd: int) -> tuple[int, int]:
    """Return (st_dev, st_ino) for an open directory descriptor."""
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys to prevent parser differentials."""
    result: dict[str, Any] = {}
    for k, v in pairs:
        if k in result:
            raise InventoryVerificationError(f"duplicate JSON field: {k!r}")
        result[k] = v
    return result


def _normalize_item(raw: Any) -> InventoryItem:
    """Validate and normalize an untrusted inventory item entry."""
    if isinstance(raw, InventoryItem):
        raw_dict: Mapping[str, Any] = {
            "object_key": raw.object_key,
            "sha256": raw.sha256,
            "size_bytes": raw.size_bytes,
            "state": raw.state,
            "object_version": raw.object_version,
        }
    elif isinstance(raw, Mapping):
        raw_dict = raw
    else:
        raise InventoryVerificationError(f"inventory entry must be a mapping, got {type(raw).__name__}")

    extra_keys = set(raw_dict.keys()) - _ALLOWED_ITEM_FIELDS
    if extra_keys:
        raise InventoryVerificationError(f"unrecognized field(s) in inventory item: {sorted(extra_keys)}")

    # Key extraction and alias reconciliation
    has_obj_key = "object_key" in raw_dict
    has_key = "key" in raw_dict
    if not has_obj_key and not has_key:
        raise InventoryVerificationError("missing object_key in inventory item")
    if has_obj_key and has_key and raw_dict["object_key"] != raw_dict["key"]:
        raise InventoryVerificationError(
            f"contradictory key aliases: object_key={raw_dict['object_key']!r} vs key={raw_dict['key']!r}"
        )
    key = raw_dict["object_key"] if has_obj_key else raw_dict["key"]
    if not isinstance(key, str):
        raise InventoryVerificationError(f"object_key must be a str, got {type(key).__name__}")
    match = _OBJECT_KEY_RE.fullmatch(key)
    if not match:
        raise InventoryVerificationError(f"invalid object_key format: {key!r}")
    prefix, digest = match.group(1), match.group(2)
    if prefix != digest[:2]:
        raise InventoryVerificationError(f"prefix {prefix!r} does not match digest prefix {digest[:2]!r} in {key!r}")

    # SHA-256 verification
    sha256_val = raw_dict.get("sha256")
    if sha256_val is not None:
        if not isinstance(sha256_val, str) or sha256_val.lower() != digest:
            raise InventoryVerificationError(f"sha256 field {sha256_val!r} does not match key digest {digest!r}")

    # Size extraction and alias reconciliation
    has_size_bytes = "size_bytes" in raw_dict
    has_size = "size" in raw_dict
    if not has_size_bytes and not has_size:
        raise InventoryVerificationError(f"missing size_bytes for object {key!r}")
    if has_size_bytes and has_size and raw_dict["size_bytes"] != raw_dict["size"]:
        raise InventoryVerificationError(
            f"contradictory size aliases: size_bytes={raw_dict['size_bytes']!r} vs size={raw_dict['size']!r}"
        )
    size_val = raw_dict["size_bytes"] if has_size_bytes else raw_dict["size"]
    if type(size_val) is not int or isinstance(size_val, bool) or size_val < 0:
        raise InventoryVerificationError(f"size_bytes must be a non-negative integer, got {size_val!r}")

    # State verification: only committed artifacts may be verified; null/None is rejected
    if "state" in raw_dict:
        state_val = raw_dict["state"]
        if state_val != "committed":
            raise InventoryVerificationError(f"inventory item state must be 'committed', got {state_val!r}")

    # Version verification
    version_val = raw_dict.get("object_version", "1")
    if not isinstance(version_val, str) or not (1 <= len(version_val) <= 256):
        raise InventoryVerificationError(f"invalid object_version: {version_val!r}")

    return InventoryItem(
        object_key=key,
        sha256=digest,
        size_bytes=size_val,
        state="committed",
        object_version=version_val,
    )


def parse_inventory(
    source: str | bytes | Path | Iterable[Any],
    *,
    max_inventory_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
    max_items: int = DEFAULT_MAX_INVENTORY_ITEMS,
    allow_empty: bool = False,
) -> list[InventoryItem]:
    """Parse, validate, and normalize an untrusted inventory source.

    Accepts:
    - Path pointing to an existing JSON or JSONL inventory file.
    - String representing a file path or raw JSON / JSONL content.
    - Raw bytes of JSON or JSONL.
    - Iterable of mappings or InventoryItem objects (bounded iteration).
    """
    raw_entries: list[Any]

    if isinstance(source, Path):
        if not source.exists():
            raise InventoryVerificationError(f"inventory file not found: {source}")
        if not source.is_file():
            raise InventoryVerificationError(f"inventory path is not a regular file: {source}")
        stat_info = source.stat()
        if stat_info.st_size > max_inventory_bytes:
            raise InventoryVerificationError(
                f"inventory file size {stat_info.st_size} exceeds limit {max_inventory_bytes}"
            )
        with source.open("rb") as f:
            content = f.read(max_inventory_bytes + 1)
        if len(content) > max_inventory_bytes:
            raise InventoryVerificationError(f"inventory file exceeds limit {max_inventory_bytes}")
        return parse_inventory(
            content,
            max_inventory_bytes=max_inventory_bytes,
            max_items=max_items,
            allow_empty=allow_empty,
        )

    if isinstance(source, str):
        p = Path(source)
        try:
            is_file = p.is_file()
        except OSError:
            is_file = False
        if is_file:
            return parse_inventory(
                p,
                max_inventory_bytes=max_inventory_bytes,
                max_items=max_items,
                allow_empty=allow_empty,
            )
        stripped = source.strip()
        if not stripped.startswith(("{", "[")) and ("/" in source or source.endswith((".json", ".jsonl"))):
            raise InventoryVerificationError(f"inventory file not found: {source}")
        return parse_inventory(
            source.encode("utf-8"),
            max_inventory_bytes=max_inventory_bytes,
            max_items=max_items,
            allow_empty=allow_empty,
        )

    if isinstance(source, bytes):
        if len(source) > max_inventory_bytes:
            raise InventoryVerificationError(f"inventory payload exceeds limit {max_inventory_bytes}")
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InventoryVerificationError("inventory payload is not valid UTF-8") from exc

        stripped = text.strip()
        if not stripped:
            if allow_empty:
                return []
            raise InventoryVerificationError("inventory payload is empty")

        # Try standard JSON first
        try:
            doc = json.loads(text, object_pairs_hook=_unique_pairs)
            if isinstance(doc, dict):
                if "format" in doc and doc["format"] != "lab-artifact-inventory-1":
                    raise InventoryVerificationError(f"unsupported inventory format: {doc['format']!r}")
                if "artifacts" in doc and isinstance(doc["artifacts"], list):
                    raw_entries = doc["artifacts"]
                elif "objects" in doc and isinstance(doc["objects"], list):
                    raw_entries = doc["objects"]
                else:
                    raise InventoryVerificationError("inventory JSON object missing 'artifacts' or 'objects' list")
            elif isinstance(doc, list):
                raw_entries = doc
            else:
                raise InventoryVerificationError("inventory JSON root must be an object or list")
        except (json.JSONDecodeError, ValueError) as json_err:
            # Fall back to JSONL with bounded count
            raw_entries = []
            for line_no, line in enumerate(text.splitlines(), 1):
                line_str = line.strip()
                if not line_str:
                    continue
                if len(raw_entries) >= max_items:
                    raise InventoryVerificationError(f"inventory count exceeds maximum {max_items}")
                try:
                    entry = json.loads(line_str, object_pairs_hook=_unique_pairs)
                except Exception as line_err:
                    raise InventoryVerificationError(
                        f"invalid inventory JSON / JSONL at line {line_no}: {line_err}"
                    ) from line_err
                if not isinstance(entry, dict):
                    raise InventoryVerificationError(f"inventory JSONL line {line_no} must be a JSON object")
                raw_entries.append(entry)
            if not raw_entries:
                raise InventoryVerificationError(f"invalid inventory format: {json_err}") from json_err

    elif isinstance(source, Iterable):
        # Bounded iteration: consume at most max_items + 1 to prevent infinite generator exhaustion/hang
        raw_entries = []
        for count, entry in enumerate(source, 1):
            if count > max_items:
                raise InventoryVerificationError(f"inventory count exceeds maximum {max_items}")
            raw_entries.append(entry)
    else:
        raise InventoryVerificationError(f"unsupported inventory source type: {type(source).__name__}")

    if len(raw_entries) > max_items:
        raise InventoryVerificationError(f"inventory count {len(raw_entries)} exceeds maximum {max_items}")
    if len(raw_entries) == 0 and not allow_empty:
        raise InventoryVerificationError("inventory must not be empty")

    seen_keys: set[str] = set()
    normalized: list[InventoryItem] = []
    for entry in raw_entries:
        item = _normalize_item(entry)
        if item.object_key in seen_keys:
            raise InventoryVerificationError(f"duplicate object_key in inventory: {item.object_key!r}")
        seen_keys.add(item.object_key)
        normalized.append(item)

    # Sort deterministically by object_key for grouped prefix traversal
    normalized.sort(key=lambda x: x.object_key)
    return normalized


def _open_root_no_follow(root: Path) -> tuple[int, tuple[int, int]]:
    """Verify that root and all its ancestor components are non-symlink directories.

    Traverses component-by-component starting from '/' with O_NOFOLLOW.
    Returns (root_fd, (dev, ino)).
    """
    if not isinstance(root, (Path, str)):
        raise InventoryVerificationError("store_root must be a Path or str")
    root_path = Path(root)
    if not root_path.is_absolute():
        raise InventoryVerificationError(f"store_root must be an absolute path: {root_path}")
    parts = [p for p in root_path.parts if p and p != "/"]
    if not parts:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        st = os.fstat(fd)
        return fd, (st.st_dev, st.st_ino)

    curr_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            try:
                st = os.stat(part, dir_fd=curr_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise InventoryVerificationError(
                    f"store_root component {part!r} does not exist: {root_path}"
                ) from exc
            except OSError as exc:
                raise InventoryVerificationError(f"cannot stat store_root component {part!r}: {exc}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise InventoryVerificationError(f"store_root component {part!r} is a symlink: {root_path}")
            if not stat.S_ISDIR(st.st_mode):
                raise InventoryVerificationError(f"store_root component {part!r} is not a directory: {root_path}")
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=curr_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise InventoryVerificationError(
                        f"store_root component {part!r} is a symlink or non-directory: {root_path}"
                    ) from exc
                raise InventoryVerificationError(f"cannot safely open store_root component {part!r}: {exc}") from exc
            os.close(curr_fd)
            curr_fd = next_fd

        st = os.fstat(curr_fd)
        return curr_fd, (st.st_dev, st.st_ino)
    except BaseException:
        os.close(curr_fd)
        raise


def _open_dir_no_follow(parent_fd: int, name: str, desc: str) -> int:
    """Open a child directory descriptor without following symlinks."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise InventoryVerificationError(f"{desc} {name!r} not found") from exc
    except OSError as exc:
        raise InventoryVerificationError(f"cannot stat {desc} {name!r}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise InventoryVerificationError(f"{desc} {name!r} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise InventoryVerificationError(f"{desc} {name!r} is not a directory")
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise InventoryVerificationError(f"{desc} {name!r} is a symlink or non-directory") from exc
        raise InventoryVerificationError(f"cannot safely open {desc} {name!r}: {exc}") from exc


def _verify_canonical_completion(
    root_path: Path,
    prefix: str,
    digest: str,
    expected_size: int,
    expected_root_id: tuple[int, int],
    expected_sha256_id: tuple[int, int],
    expected_prefix_id: tuple[int, int],
    expected_file_id: tuple[int, int],
) -> None:
    """Re-traverse canonical path from / to ensure no directory or file was moved/swapped."""
    c_root_fd, c_root_id = _open_root_no_follow(root_path)
    try:
        if c_root_id != expected_root_id:
            raise InventoryVerificationError(
                f"canonical store_root directory identity changed during verification of sha256/{prefix}/{digest}"
            )
        c_sha256_fd = _open_dir_no_follow(c_root_fd, "sha256", "canonical sha256 directory")
        try:
            if _dir_id(c_sha256_fd) != expected_sha256_id:
                raise InventoryVerificationError(
                    f"canonical sha256 directory identity changed during verification of sha256/{prefix}/{digest}"
                )
            c_prefix_fd = _open_dir_no_follow(c_sha256_fd, prefix, f"canonical prefix directory {prefix!r}")
            try:
                if _dir_id(c_prefix_fd) != expected_prefix_id:
                    raise InventoryVerificationError(
                        f"canonical prefix directory identity changed during verification of sha256/{prefix}/{digest}"
                    )
                try:
                    c_st = os.stat(digest, dir_fd=c_prefix_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise InventoryVerificationError(
                        f"canonical artifact file missing after read: sha256/{prefix}/{digest}"
                    ) from exc
                except OSError as exc:
                    raise InventoryVerificationError(
                        f"cannot stat canonical artifact file sha256/{prefix}/{digest}: {exc}"
                    ) from exc

                if stat.S_ISLNK(c_st.st_mode):
                    raise InventoryVerificationError(
                        f"canonical artifact is a symlink: sha256/{prefix}/{digest}"
                    )
                if not stat.S_ISREG(c_st.st_mode):
                    raise InventoryVerificationError(
                        f"canonical artifact is not a regular file: sha256/{prefix}/{digest}"
                    )
                if (c_st.st_dev, c_st.st_ino) != expected_file_id:
                    raise InventoryVerificationError(
                        f"canonical artifact file identity changed during verification: sha256/{prefix}/{digest}"
                    )
                if c_st.st_size != expected_size:
                    raise InventoryVerificationError(
                        f"canonical artifact file size changed: expected {expected_size}, got {c_st.st_size}"
                    )
            finally:
                os.close(c_prefix_fd)
        finally:
            os.close(c_sha256_fd)
    finally:
        os.close(c_root_fd)


def verify_restored_inventory(
    inventory: str | bytes | Path | Iterable[Any],
    store_root: str | Path,
    *,
    max_inventory_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
    max_items: int = DEFAULT_MAX_INVENTORY_ITEMS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    allow_empty: bool = False,
) -> VerificationReport:
    """Verify a committed artifact inventory against local restored bytes.

    Strictly read-only; fails closed on any malformed input, missing object,
    hash mismatch, size mismatch, non-regular file, symlink traversal,
    or directory/file swap race condition.
    """
    items = parse_inventory(
        inventory,
        max_inventory_bytes=max_inventory_bytes,
        max_items=max_items,
        allow_empty=allow_empty,
    )

    root_path = Path(store_root)
    root_fd, (orig_root_dev, orig_root_ino) = _open_root_no_follow(root_path)
    orig_root_id = (orig_root_dev, orig_root_ino)

    # Check aggregate expected bytes bound before any reading
    total_expected = sum(item.size_bytes for item in items)
    if total_expected > max_total_bytes:
        raise InventoryVerificationError(
            f"aggregate inventory bytes {total_expected} exceeds limit {max_total_bytes}"
        )

    total_bytes = 0
    verified_keys: list[str] = []

    try:
        if not items:
            return VerificationReport(
                verified_count=0,
                total_bytes=0,
                store_root=str(root_path),
                verified_keys=(),
            )

        sha256_fd = _open_dir_no_follow(root_fd, "sha256", "store component")
        orig_sha256_id = _dir_id(sha256_fd)

        try:
            curr_prefix: str | None = None
            prefix_fd: int | None = None
            orig_prefix_id: tuple[int, int] | None = None

            try:
                for item in items:
                    if item.size_bytes > max_file_bytes:
                        raise InventoryVerificationError(
                            f"item {item.object_key} size {item.size_bytes} exceeds limit {max_file_bytes}"
                        )
                    if total_bytes + item.size_bytes > max_total_bytes:
                        raise InventoryVerificationError(
                            f"aggregate verified bytes {total_bytes + item.size_bytes} exceeds limit {max_total_bytes}"
                        )

                    prefix = item.sha256[:2]
                    digest = item.sha256

                    if prefix != curr_prefix:
                        if prefix_fd is not None:
                            os.close(prefix_fd)
                            prefix_fd = None
                        prefix_fd = _open_dir_no_follow(sha256_fd, prefix, f"prefix directory {prefix!r}")
                        orig_prefix_id = _dir_id(prefix_fd)
                        curr_prefix = prefix

                    assert orig_prefix_id is not None

                    # Stat the digest file inside prefix_fd with follow_symlinks=False
                    try:
                        st_obj = os.stat(digest, dir_fd=prefix_fd, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise InventoryVerificationError(
                            f"artifact not found in restored store: {item.object_key}"
                        ) from exc
                    except OSError as exc:
                        raise InventoryVerificationError(
                            f"cannot stat artifact {item.object_key}: {exc}"
                        ) from exc

                    if stat.S_ISLNK(st_obj.st_mode):
                        raise InventoryVerificationError(f"artifact {item.object_key} is a symlink")
                    if not stat.S_ISREG(st_obj.st_mode):
                        raise InventoryVerificationError(f"artifact {item.object_key} is not a regular file")
                    if st_obj.st_size != item.size_bytes:
                        raise InventoryVerificationError(
                            f"size mismatch for {item.object_key}: expected {item.size_bytes}, got {st_obj.st_size}"
                        )

                    # Open file descriptor with O_NOFOLLOW and O_NONBLOCK (prevents hang on FIFO)
                    try:
                        file_fd = os.open(
                            digest,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                            dir_fd=prefix_fd,
                        )
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise InventoryVerificationError(
                                f"artifact {item.object_key} is a symlink or non-regular file"
                            ) from exc
                        raise InventoryVerificationError(
                            f"cannot safely open artifact {item.object_key}: {exc}"
                        ) from exc

                    try:
                        st_opened = os.fstat(file_fd)
                        if not stat.S_ISREG(st_opened.st_mode):
                            raise InventoryVerificationError(
                                f"artifact {item.object_key} is not a regular file"
                            )
                        if st_opened.st_size != item.size_bytes:
                            raise InventoryVerificationError(
                                f"size mismatch for {item.object_key}: expected {item.size_bytes}, got {st_opened.st_size}"
                            )
                        orig_file_id = (st_opened.st_dev, st_opened.st_ino)

                        # Stream content and compute SHA-256
                        hasher = hashlib.sha256()
                        bytes_read = 0
                        with os.fdopen(os.dup(file_fd), "rb") as stream:
                            while True:
                                chunk = stream.read(READ_CHUNK_SIZE)
                                if not chunk:
                                    break
                                bytes_read += len(chunk)
                                if bytes_read > item.size_bytes:
                                    raise InventoryVerificationError(
                                        f"file size grew beyond {item.size_bytes} during read: {item.object_key}"
                                    )
                                if bytes_read > max_file_bytes:
                                    raise InventoryVerificationError(
                                        f"file size exceeds limit {max_file_bytes}: {item.object_key}"
                                    )
                                if total_bytes + bytes_read > max_total_bytes:
                                    raise InventoryVerificationError(
                                        f"aggregate verified bytes {total_bytes + bytes_read} exceeds limit {max_total_bytes}"
                                    )
                                hasher.update(chunk)

                        if bytes_read != item.size_bytes:
                            raise InventoryVerificationError(
                                f"size mismatch for {item.object_key}: expected {item.size_bytes}, got {bytes_read}"
                            )
                        actual_digest = hasher.hexdigest()
                        if actual_digest != digest:
                            raise InventoryVerificationError(
                                f"hash mismatch for {item.object_key}: expected {digest}, got {actual_digest}"
                            )

                        # Re-verify canonical reachability and identity down to the file
                        _verify_canonical_completion(
                            root_path=root_path,
                            prefix=prefix,
                            digest=digest,
                            expected_size=item.size_bytes,
                            expected_root_id=orig_root_id,
                            expected_sha256_id=orig_sha256_id,
                            expected_prefix_id=orig_prefix_id,
                            expected_file_id=orig_file_id,
                        )

                        total_bytes += bytes_read
                        verified_keys.append(item.object_key)
                    finally:
                        os.close(file_fd)

            finally:
                if prefix_fd is not None:
                    os.close(prefix_fd)
        finally:
            os.close(sha256_fd)

        # Confirm store_root canonical identity remains unchanged
        verify_fd, (curr_dev, curr_ino) = _open_root_no_follow(root_path)
        os.close(verify_fd)
        if (curr_dev, curr_ino) != orig_root_id:
            raise InventoryVerificationError("store_root directory identity changed during verification")

    finally:
        os.close(root_fd)

    return VerificationReport(
        verified_count=len(verified_keys),
        total_bytes=total_bytes,
        store_root=str(root_path),
        verified_keys=tuple(verified_keys),
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for restore inventory verification."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.governance.restore_inventory",
        description="Verify restored local artifact bytes against a supplied committed artifact inventory.",
    )
    parser.add_argument(
        "pos_inventory",
        nargs="?",
        default=None,
        help="Path to inventory file (JSON or JSONL format).",
    )
    parser.add_argument(
        "pos_store_root",
        nargs="?",
        default=None,
        help="Path to restored store root directory (must contain 'sha256' layout).",
    )
    parser.add_argument(
        "--inventory",
        "-i",
        dest="flag_inventory",
        help="Path to inventory file (JSON or JSONL format).",
    )
    parser.add_argument(
        "--store-root",
        "-s",
        dest="flag_store_root",
        help="Path to restored store root directory.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_INVENTORY_ITEMS,
        help=f"Maximum allowed items in inventory (default {DEFAULT_MAX_INVENTORY_ITEMS}).",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=f"Maximum allowed size per artifact file in bytes (default {DEFAULT_MAX_FILE_BYTES}).",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
        help=f"Maximum allowed aggregate verified bytes (default {DEFAULT_MAX_TOTAL_BYTES}).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty inventory without error (default rejects empty inventory).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON report on success.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for operator-facing restored inventory verification."""
    parser = build_parser()
    args = parser.parse_args(argv)

    inventory_path = args.flag_inventory or args.pos_inventory
    store_root = args.flag_store_root or args.pos_store_root

    if not inventory_path or not store_root:
        parser.print_usage(sys.stderr)
        print("error: both inventory and store_root must be provided", file=sys.stderr)
        return 2

    try:
        report = verify_restored_inventory(
            inventory_path,
            store_root,
            max_items=args.max_items,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
            allow_empty=args.allow_empty,
        )
    except InventoryVerificationError as exc:
        print(f"restore-inventory verification failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"restore-inventory unexpected error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"Verified {report.verified_count} committed artifact(s) "
            f"({report.total_bytes} bytes) against {report.store_root}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
