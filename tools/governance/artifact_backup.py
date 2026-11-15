"""Bounded offline bundle for explicitly selected local SHA-256 objects.

This is a byte transport primitive, not a database-consistent backup. Callers
must obtain and retain an authenticated object list from a consistent snapshot.
The source and destination parents must be private to the operator.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

MAX_OBJECTS = 1000
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 90 * 1024 * 1024
_KEY = re.compile(r"\Asha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")


class BackupError(ValueError):
    """Malformed, unsafe or corrupt input; no success may be inferred."""


def _digest(key: object) -> str:
    if not isinstance(key, str) or not (m := _KEY.fullmatch(key)) or m[1] != m[2][:2]:
        raise BackupError("invalid object key")
    return m[2]


def _open_object(root: Path, key: str) -> bytes:
    # O_NOFOLLOW applies to each component, including the root itself.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = []
    try:
        fd = os.open(root, flags)
        fds.append(fd)
        for part in key.split('/')[:-1]:
            fd = os.open(part, flags, dir_fd=fd)
            fds.append(fd)
        fd = os.open(key.split('/')[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        fds.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OBJECT_BYTES:
            raise BackupError("object is not a bounded regular file")
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(MAX_OBJECT_BYTES + 1)
        if len(data) > MAX_OBJECT_BYTES or len(data) != info.st_size:
            raise BackupError("object size changed or exceeds limit")
        if hashlib.sha256(data).hexdigest() != _digest(key):
            raise BackupError("object digest mismatch")
        return data
    except OSError as exc:
        raise BackupError("cannot safely open object") from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def create_bundle(root: Path, keys: list[str]) -> bytes:
    """Export a caller-supplied selection; it does not discover or authorize objects."""
    if not isinstance(keys, list) or len(keys) > MAX_OBJECTS:
        raise BackupError("invalid object list")
    for key in keys:
        _digest(key)
    if len(set(keys)) != len(keys):
        raise BackupError("duplicate object key")
    entries = []
    total = 0
    for key in sorted(keys):
        data = _open_object(Path(root), key)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BackupError("total object bytes exceed limit")
        entries.append({'key': key, 'size': len(data), 'data': base64.b64encode(data).decode('ascii')})
    bundle = json.dumps({'format': 'lab-artifact-bundle-1', 'objects': entries}, sort_keys=True, separators=(',', ':')).encode('ascii')
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise BackupError("bundle exceeds limit")
    return bundle


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for k, v in pairs:
        if k in result:
            raise BackupError("duplicate JSON field")
        result[k] = v
    return result


def restore_bundle(bundle: bytes, destination: Path) -> int:
    """Validate first, then restore into a newly created private destination.

    The parent is trusted and must not be concurrently modified. Existing
    destinations are never replaced. On a write failure the new tree is removed
    best-effort; it must not be consumed unless this function returns normally.
    """
    if not isinstance(bundle, bytes) or len(bundle) > MAX_BUNDLE_BYTES:
        raise BackupError("invalid bundle size")
    try:
        doc = json.loads(bundle, object_pairs_hook=_unique_pairs)
    except BackupError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BackupError("invalid bundle JSON") from exc
    if not isinstance(doc, dict) or set(doc) != {'format', 'objects'} or doc['format'] != 'lab-artifact-bundle-1':
        raise BackupError("unsupported bundle")
    objects = doc['objects']
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise BackupError("invalid object list")
    decoded = []
    seen = set()
    total = 0
    for item in objects:
        if not isinstance(item, dict) or set(item) != {'key', 'size', 'data'}:
            raise BackupError("invalid entry")
        key, size, encoded = item['key'], item['size'], item['data']
        digest = _digest(key)
        if key in seen or type(size) is not int or size < 0 or size > MAX_OBJECT_BYTES or not isinstance(encoded, str):
            raise BackupError("invalid entry metadata")
        seen.add(key)
        total += size
        if total > MAX_TOTAL_BYTES or len(encoded) != 4 * ((size + 2) // 3):
            raise BackupError("invalid entry size")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BackupError("invalid base64") from exc
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise BackupError("object digest or size mismatch")
        decoded.append((key, data))
    destination = Path(destination)
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise BackupError("destination cannot be created") from exc
    try:
        for key, data in decoded:
            target = destination.joinpath(*key.split('/'))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
        return len(decoded)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
