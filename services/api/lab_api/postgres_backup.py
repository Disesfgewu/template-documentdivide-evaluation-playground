"""PostgreSQL custom-format archive backup and disposable-database restore verification.

This module implements a scoped operator primitive for Issue #14 (SPEC §10.2,
R24, T79, A29 foundation) covering:
1. Creating a custom-format ``pg_dump`` archive (``--format=custom``) of a
   source PostgreSQL database into a newly created private directory, streaming
   bounded SHA-256 and byte-size calculation without buffering the entire
   archive in RAM, and writing a companion JSON integrity manifest.
2. Verifying a backup archive's integrity manifest, size, SHA-256 digest, and
   ``PGDMP`` custom-format header, holding the verified regular file open via a
   securely held file descriptor (or passing it via ``stdin`` / ``pass_fds``
   ``/dev/fd/<fd>``) so ``pg_restore`` never reopens a mutable pathname after
   verification, and restoring it only into an explicitly identified, distinct,
   disposable, and verified-empty target PostgreSQL database, followed by
   post-restore verification queries.

Security, trust boundary, and safety invariants:
- Never interpolates shell commands; always invokes fixed argument vectors with
  ``shell=False``.
- Never places database URLs, passwords, or query parameters in process
  argument vectors, logs, returned metadata, or exception messages. Connection
  parameters are passed only through a minimal subprocess environment
  (``PGHOST``, ``PGPORT``, ``PGDATABASE``, ``PGUSER``, ``PGPASSWORD``,
  ``PGSSLMODE``, ``PGCONNECT_TIMEOUT``, ``PGOPTIONS``).
- Never creates or drops a database (no ``CREATE DATABASE``, ``DROP DATABASE``,
  ``--create``, or ``--clean``).
- Prevents accidental production or destructive restores:
  - Requires an explicit ``confirm_disposable_target_database`` argument that
    must equal the parsed target database name.
  - Rejects target database names unless they carry an explicit disposable test
    or restore prefix (``lab_restore_``, ``lab_verify_``, ``disposable_``,
    ``restore_verify_``, or ``tmp_restore_``).
  - Rejects restore if the target database name or its SHA-256 fingerprint
    matches either the explicit source database or ``source_database_sha256``
    recorded in ``manifest.json``, regardless of hostname (preventing bypasses
    via DNS aliases or alternate loopback addresses such as ``127.0.0.2``), in
    addition to rejecting matching ``source_endpoint_sha256``.
  - Queries the real target database via ``psycopg`` before ``pg_restore``
    (while holding a session-level advisory lock during the emptiness check and
    verifying ``pg_stat_activity`` has no other active connections to the target
    database) and fails closed if any non-system user relations, user
    types/domains, user routines/procedures, event triggers, user schemas (other
    than an empty ``public`` schema), or non-allowlisted extensions exist.
- Rejects relative paths, ``..`` segments, non-regular files, and symlinks on
  every path component (using ``O_NOFOLLOW`` directory traversal).
- Bounds subprocess wall time, statement timeout, lock timeout, archive byte
  size, manifest byte size, and captured stdout/stderr byte counts, killing the
  subprocess group on timeout or output overflow and cleaning up temporary or
  failed output paths best-effort.

Explicit non-claims and operator obligations:
- **Digest is not authentication, and no SQL sandbox is claimed**: ``manifest.json``
  records an unkeyed SHA-256 digest solely to detect accidental corruption,
  truncation, or local file mismatch. SHA-256 does NOT authenticate the origin
  of a backup archive, and restoring a PostgreSQL archive via ``pg_restore`` can
  execute arbitrary SQL/DDL/functions as the restore database role. The backup
  directory and archive MUST come from a trusted operator source and be stored in
  operator-private storage.
- **Target database isolation (empty-target TOCTOU)**: Although
  ``verify_postgres_restore`` checks that the target database is empty and has
  no other concurrent client backends in ``pg_stat_activity`` before invoking
  ``pg_restore`` in a separate subprocess connection, a third party with
  credentials to the target database could still connect between the emptiness
  check and ``pg_restore`` execution. Operators must dedicate the disposable
  target database exclusively to a single restore verification run.
- **Not a coordinated cross-store recovery point**: An independent ``pg_dump``
  archive (even when paired with an offline artifact bundle) is NOT a
  coordinated recovery point across PostgreSQL and artifact storage. Concurrent
  writes between or during database dump and artifact collection can leave
  references skewed. Coordinated recovery points, full schema/migration/lineage/
  score-to-PDF restore verification (T79/A29), and measured deployment RPO/RTO
  remain open in #5, #7, #14, and #15.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg

MANIFEST_FORMAT = "lab-pg-backup-manifest-v1"
ARCHIVE_FILENAME = "database.pgdump"
MANIFEST_FILENAME = "manifest.json"
PGDMP_MAGIC = b"PGDMP"
STREAM_CHUNK_BYTES = 64 * 1024

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_TOOL_OUTPUT_BYTES = 256 * 1024

ALLOWED_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)
ALLOWED_DISPOSABLE_TARGET_PREFIXES = (
    "lab_restore_",
    "lab_verify_",
    "disposable_",
    "restore_verify_",
    "tmp_restore_",
)
ALLOWED_PREINSTALLED_EXTENSIONS = frozenset({"plpgsql", "vector"})

_SAFE_DB_IDENT = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_-]{0,62}\Z")
_SAFE_USER_IDENT = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_FORBIDDEN_TARGET_NAMES = frozenset(
    {"postgres", "template0", "template1", "lab", "prod", "production", "main", "default"}
)


class PostgresBackupError(ValueError):
    """Backup or restore verification failed; message contains only a fixed code."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            code = "backup_failed"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BackupLimits:
    """Explicit bounds for PostgreSQL backup and restore verification."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: int = 5
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_tool_output_bytes: int = DEFAULT_MAX_TOOL_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not (0 < self.timeout_seconds <= MAX_TIMEOUT_SECONDS)
        ):
            raise PostgresBackupError("invalid_limits")
        if (
            isinstance(self.connect_timeout_seconds, bool)
            or not isinstance(self.connect_timeout_seconds, int)
            or not (1 <= self.connect_timeout_seconds <= 30)
        ):
            raise PostgresBackupError("invalid_limits")
        if (
            isinstance(self.max_archive_bytes, bool)
            or not isinstance(self.max_archive_bytes, int)
            or not (1024 <= self.max_archive_bytes <= 2 * 1024 * 1024 * 1024)
        ):
            raise PostgresBackupError("invalid_limits")
        if (
            isinstance(self.max_tool_output_bytes, bool)
            or not isinstance(self.max_tool_output_bytes, int)
            or not (1024 <= self.max_tool_output_bytes <= 16 * 1024 * 1024)
        ):
            raise PostgresBackupError("invalid_limits")


@dataclass(frozen=True)
class BackupManifest:
    """Integrity metadata for a single custom-format pg_dump archive."""

    format: str
    archive_filename: str
    archive_sha256: str
    archive_size_bytes: int
    source_endpoint_sha256: str
    source_database_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "archive_filename": self.archive_filename,
            "archive_sha256": self.archive_sha256,
            "archive_size_bytes": self.archive_size_bytes,
            "source_endpoint_sha256": self.source_endpoint_sha256,
            "source_database_sha256": self.source_database_sha256,
        }


@dataclass(frozen=True)
class RestoreVerificationReport:
    """Summary of a verified restore into a disposable target database."""

    archive_sha256: str
    archive_size_bytes: int
    restored_catalog_entry_count: int
    restored_user_table_count: int
    restored_user_object_count: int


@dataclass(frozen=True)
class _PgEndpoint:
    host: str
    port: int
    database: str
    user: str
    password: str | None
    sslmode: str | None

    @property
    def canonical_host(self) -> str:
        h = self.host.strip().lower()
        if h in {"localhost", "127.0.0.1", "::1"}:
            return "loopback"
        return h

    @property
    def endpoint_fingerprint(self) -> str:
        payload = f"{self.canonical_host}:{self.port}/{self.database}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def database_fingerprint(self) -> str:
        return hashlib.sha256(self.database.encode("utf-8")).hexdigest()

    def connect_kwargs(self, limits: BackupLimits) -> dict[str, object]:
        statement_ms = max(100, min(int(limits.timeout_seconds * 1000), 300_000))
        kw: dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "connect_timeout": limits.connect_timeout_seconds,
            "options": (
                f"-c statement_timeout={statement_ms} "
                f"-c lock_timeout={statement_ms} "
                "-c idle_in_transaction_session_timeout=10000"
            ),
            "autocommit": True,
        }
        if self.password is not None:
            kw["password"] = self.password
        if self.sslmode is not None:
            kw["sslmode"] = self.sslmode
        return kw

    def subprocess_env(self, limits: BackupLimits) -> dict[str, str]:
        statement_ms = max(100, min(int(limits.timeout_seconds * 1000), 300_000))
        env: dict[str, str] = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGDATABASE": self.database,
            "PGUSER": self.user,
            "PGCONNECT_TIMEOUT": str(limits.connect_timeout_seconds),
            "PGOPTIONS": (
                f"-c statement_timeout={statement_ms} "
                f"-c lock_timeout={statement_ms}"
            ),
        }
        if self.password is not None:
            env["PGPASSWORD"] = self.password
        if self.sslmode is not None:
            env["PGSSLMODE"] = self.sslmode
        return env


def _parse_pg_url(raw_url: object) -> _PgEndpoint:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 2048:
        raise PostgresBackupError("invalid_database_url")
    if any(ch in raw_url for ch in ("\x00", "\r", "\n")):
        raise PostgresBackupError("invalid_database_url")
    try:
        parts = urlsplit(raw_url)
        scheme = parts.scheme
        hostname = parts.hostname
        port = parts.port
        username = parts.username
        password = parts.password
        path = parts.path
        query = parts.query
        fragment = parts.fragment
    except Exception:
        raise PostgresBackupError("invalid_database_url") from None

    if scheme not in ("postgresql", "postgres"):
        raise PostgresBackupError("invalid_database_url")
    if fragment or not hostname:
        raise PostgresBackupError("invalid_database_url")
    if any(ch in hostname for ch in ("\x00", "/", "\\", " ", "\t")):
        raise PostgresBackupError("invalid_database_url")

    resolved_port = 5432 if port is None else port
    if not (1 <= resolved_port <= 65535):
        raise PostgresBackupError("invalid_database_url")

    if not path.startswith("/") or path.count("/") != 1:
        raise PostgresBackupError("invalid_database_url")
    dbname = unquote(path[1:])
    if not _SAFE_DB_IDENT.fullmatch(dbname):
        raise PostgresBackupError("invalid_database_url")

    if not username:
        raise PostgresBackupError("invalid_database_url")
    user = unquote(username)
    if not _SAFE_USER_IDENT.fullmatch(user):
        raise PostgresBackupError("invalid_database_url")

    decoded_password: str | None = None
    if password is not None:
        decoded_password = unquote(password)
        if "\x00" in decoded_password or "\n" in decoded_password or "\r" in decoded_password:
            raise PostgresBackupError("invalid_database_url")

    sslmode: str | None = None
    if query:
        try:
            qpairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise PostgresBackupError("invalid_database_url") from None
        for k, v in qpairs:
            if k == "sslmode" and sslmode is None and v in ALLOWED_SSLMODES:
                sslmode = v
            else:
                # Reject arbitrary libpq query parameters (e.g. service, passfile,
                # sslrootcert, options, replication) to prevent file/option injection.
                raise PostgresBackupError("unsupported_url_parameter")

    return _PgEndpoint(
        host=hostname,
        port=resolved_port,
        database=dbname,
        user=user,
        password=decoded_password,
        sslmode=sslmode,
    )


def _validate_disposable_target(
    target: _PgEndpoint,
    confirm_disposable_target_database: object,
    source: _PgEndpoint | None = None,
    manifest: BackupManifest | None = None,
) -> None:
    if not isinstance(confirm_disposable_target_database, str) or not confirm_disposable_target_database:
        raise PostgresBackupError("missing_target_confirmation")
    if confirm_disposable_target_database != target.database:
        raise PostgresBackupError("target_confirmation_mismatch")

    lower_db = target.database.lower()
    if lower_db in _FORBIDDEN_TARGET_NAMES:
        raise PostgresBackupError("unsafe_target_database_name")
    if not any(lower_db.startswith(prefix) for prefix in ALLOWED_DISPOSABLE_TARGET_PREFIXES):
        raise PostgresBackupError("unsafe_target_database_name")

    if source is not None:
        # Reject same database name or database hash regardless of hostname
        # (prevents bypasses via DNS aliases or alternate loopback IPs like 127.0.0.2).
        if (
            source.database == target.database
            or source.database.lower() == target.database.lower()
            or source.database_fingerprint == target.database_fingerprint
            or source.endpoint_fingerprint == target.endpoint_fingerprint
        ):
            raise PostgresBackupError("target_matches_source")

    if manifest is not None:
        if (
            manifest.source_database_sha256 == target.database_fingerprint
            or manifest.source_endpoint_sha256 == target.endpoint_fingerprint
        ):
            raise PostgresBackupError("target_matches_source")


def _validate_clean_absolute_path(path: object) -> Path:
    if not isinstance(path, (str, Path)):
        raise PostgresBackupError("unsafe_path")
    raw = str(path)
    if not raw or "\x00" in raw:
        raise PostgresBackupError("unsafe_path")
    p = Path(raw)
    if not p.is_absolute():
        raise PostgresBackupError("unsafe_path")
    if any(part in ("..", ".") for part in p.parts):
        raise PostgresBackupError("unsafe_path")
    return p


def _open_dir_nofollow(path: Path) -> int:
    """Open an existing directory without following symlinks on any path component."""
    p = _validate_clean_absolute_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds: list[int] = []
    try:
        fd = os.open("/", flags)
        fds.append(fd)
        for part in p.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            fds.append(next_fd)
            fd = next_fd
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise PostgresBackupError("unsafe_path")
        keep = fds.pop()
        return keep
    except PostgresBackupError:
        raise
    except OSError:
        raise PostgresBackupError("unsafe_path") from None
    finally:
        for d in reversed(fds):
            try:
                os.close(d)
            except OSError:
                pass


def _read_regular_file_at(dir_fd: int, name: str, max_bytes: int) -> bytes:
    if not name or "/" in name or name in (".", "..") or "\x00" in name:
        raise PostgresBackupError("unsafe_path")
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PostgresBackupError("unsafe_path")
        if info.st_size < 0 or info.st_size > max_bytes:
            raise PostgresBackupError("size_limit_exceeded")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes or len(data) != info.st_size:
            raise PostgresBackupError("size_limit_exceeded")
        return data
    except PostgresBackupError:
        raise
    except OSError:
        raise PostgresBackupError("unsafe_path") from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _stream_verify_archive_fd(fd: int, max_bytes: int) -> tuple[int, str, bytes]:
    """Stream-hash an open archive descriptor without buffering the whole file in RAM.

    Verifies that ``fd`` refers to a single-link regular file within ``max_bytes``,
    reads it in fixed-size chunks from offset 0, checks that the byte count
    remained stable during the read, and returns ``(size_bytes, sha256_hex, magic_prefix)``.
    """
    try:
        info_before = os.fstat(fd)
        if not stat.S_ISREG(info_before.st_mode) or info_before.st_nlink != 1:
            raise PostgresBackupError("unsafe_path")
        if info_before.st_size < 0 or info_before.st_size > max_bytes:
            raise PostgresBackupError("size_limit_exceeded")

        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        header = bytearray()
        while True:
            chunk = os.read(fd, STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if len(header) < len(PGDMP_MAGIC):
                need = len(PGDMP_MAGIC) - len(header)
                header.extend(chunk[:need])
            total += len(chunk)
            if total > max_bytes:
                raise PostgresBackupError("size_limit_exceeded")
            digest.update(chunk)

        info_after = os.fstat(fd)
        if (
            info_after.st_ino != info_before.st_ino
            or info_after.st_dev != info_before.st_dev
            or info_after.st_nlink != 1
            or total != info_before.st_size
            or total != info_after.st_size
        ):
            raise PostgresBackupError("archive_size_mismatch")
        os.lseek(fd, 0, os.SEEK_SET)
        return total, digest.hexdigest(), bytes(header)
    except PostgresBackupError:
        raise
    except OSError:
        raise PostgresBackupError("unsafe_path") from None


def _resolve_executable(tool: object) -> str:
    if not isinstance(tool, (str, Path)):
        raise PostgresBackupError("tool_unavailable")
    raw = str(tool)
    if not raw or "\x00" in raw:
        raise PostgresBackupError("tool_unavailable")
    if "/" in raw:
        candidate = _validate_clean_absolute_path(raw)
        is_explicit_path = True
    else:
        found = shutil.which(raw)
        if not found:
            raise PostgresBackupError("tool_unavailable")
        candidate = _validate_clean_absolute_path(found)
        is_explicit_path = False

    parent_fd = _open_dir_nofollow(candidate.parent)
    fd = -1
    try:
        open_flags = os.O_RDONLY | os.O_NONBLOCK
        if is_explicit_path:
            open_flags |= os.O_NOFOLLOW
        fd = os.open(
            candidate.name,
            open_flags,
            dir_fd=parent_fd,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PostgresBackupError("unsafe_executable")
        if (info.st_mode & 0o111) == 0:
            raise PostgresBackupError("tool_unavailable")
        return str(candidate)
    except PostgresBackupError:
        raise
    except OSError as exc:
        if is_explicit_path and exc.errno == errno.ELOOP:
            raise PostgresBackupError("unsafe_executable") from None
        raise PostgresBackupError("tool_unavailable") from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise PostgresBackupError("invalid_manifest")
        out[k] = v
    return out


class _BoundedReader:
    """Drain a subprocess pipe up to a strict byte ceiling."""

    def __init__(self, stream, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self.chunks: list[bytes] = []
        self.total_bytes = 0
        self.overflowed = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if self.total_bytes > self._max_bytes:
                    self.overflowed = True
                    break
                self.chunks.append(chunk)
        except OSError:
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def join(self, timeout: float = 2.0) -> bytes:
        self._thread.join(timeout=timeout)
        return b"".join(self.chunks)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _run_bounded_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    limits: BackupLimits,
    stdin_fd: int | None = None,
    stdout_fd: int | None = None,
    max_stdout_bytes: int | None = None,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise PostgresBackupError("invalid_command")

    stdin_target = subprocess.DEVNULL if stdin_fd is None else stdin_fd
    stdout_target = subprocess.PIPE if stdout_fd is None else stdout_fd
    stdout_cap = limits.max_tool_output_bytes if max_stdout_bytes is None else max_stdout_bytes
    deadline = time.monotonic() + limits.timeout_seconds

    try:
        proc = subprocess.Popen(
            argv,
            stdin=stdin_target,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd="/",
            shell=False,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except OSError:
        raise PostgresBackupError("tool_execution_failed") from None

    out_reader: _BoundedReader | None = None
    if stdout_fd is None and proc.stdout is not None:
        out_reader = _BoundedReader(proc.stdout, stdout_cap)
        out_reader.start()
    err_reader = _BoundedReader(proc.stderr, limits.max_tool_output_bytes)
    err_reader.start()

    timed_out = False
    overflowed = False
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_group(proc)
                break
            if err_reader.overflowed or (out_reader is not None and out_reader.overflowed):
                overflowed = True
                _terminate_process_group(proc)
                break
            if stdout_fd is not None:
                try:
                    if os.fstat(stdout_fd).st_size > stdout_cap:
                        overflowed = True
                        _terminate_process_group(proc)
                        break
                except OSError:
                    overflowed = True
                    _terminate_process_group(proc)
                    break
            time.sleep(0.01)
    finally:
        _terminate_process_group(proc)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    stdout_bytes = out_reader.join() if out_reader is not None else b""
    err_reader.join()

    if timed_out:
        raise PostgresBackupError("tool_timeout")
    if overflowed or err_reader.overflowed or (out_reader is not None and out_reader.overflowed):
        raise PostgresBackupError("output_limit_exceeded")
    if stdout_fd is not None:
        try:
            if os.fstat(stdout_fd).st_size > stdout_cap:
                raise PostgresBackupError("size_limit_exceeded")
        except OSError:
            raise PostgresBackupError("backup_io_failed") from None
    if proc.returncode != 0:
        raise PostgresBackupError("tool_failed")
    return stdout_bytes


def _check_source_reachable(endpoint: _PgEndpoint, limits: BackupLimits) -> None:
    try:
        with psycopg.connect(**endpoint.connect_kwargs(limits)) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
    except psycopg.Error:
        raise PostgresBackupError("source_connection_failed") from None
    if not row or row[0] != endpoint.database:
        raise PostgresBackupError("source_endpoint_mismatch")


def _verify_target_database_empty(endpoint: _PgEndpoint, limits: BackupLimits) -> None:
    try:
        with psycopg.connect(**endpoint.connect_kwargs(limits)) as conn:
            db_row = conn.execute(
                "SELECT current_database(), pg_is_in_recovery()"
            ).fetchone()
            if not db_row or db_row[0] != endpoint.database or db_row[1] is not False:
                raise PostgresBackupError("target_endpoint_mismatch")

            # Fail closed if any other backend is concurrently connected to the
            # disposable target database during verification.
            other_backends = conn.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                """
            ).fetchone()[0]
            if other_backends != 0:
                raise PostgresBackupError("target_in_use")

            # 1. No user schemas other than 'public'.
            extra_schemas = conn.execute(
                """
                SELECT count(*)
                FROM pg_namespace
                WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'public')
                  AND nspname NOT LIKE 'pg_temp_%'
                  AND nspname NOT LIKE 'pg_toast_temp_%'
                """
            ).fetchone()[0]
            if extra_schemas != 0:
                raise PostgresBackupError("target_not_empty")

            # 2. No non-extension user relations (tables, views, sequences, matviews,
            # foreign tables, partitioned tables) in any non-system schema.
            user_relations = conn.execute(
                """
                SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid
                        AND d.deptype = 'e'
                  )
                """
            ).fetchone()[0]
            if user_relations != 0:
                raise PostgresBackupError("target_not_empty")

            # 3. No non-extension user routines/procedures in non-system schemas.
            user_procs = conn.execute(
                """
                SELECT count(*)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend d
                      WHERE d.classid = 'pg_proc'::regclass
                        AND d.objid = p.oid
                        AND d.deptype = 'e'
                  )
                """
            ).fetchone()[0]
            if user_procs != 0:
                raise PostgresBackupError("target_not_empty")

            # 4. No non-extension user-defined types/domains in non-system schemas.
            user_types = conn.execute(
                """
                SELECT count(*)
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
                  AND t.typtype IN ('c', 'd', 'e', 'r', 'm')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend d
                      WHERE d.classid = 'pg_type'::regclass
                        AND d.objid = t.oid
                        AND d.deptype = 'e'
                  )
                """
            ).fetchone()[0]
            if user_types != 0:
                raise PostgresBackupError("target_not_empty")

            # 5. No user event triggers.
            event_triggers = conn.execute(
                "SELECT count(*) FROM pg_event_trigger"
            ).fetchone()[0]
            if event_triggers != 0:
                raise PostgresBackupError("target_not_empty")

            # 6. Only allowlisted preinstalled extensions (plpgsql, vector).
            ext_rows = conn.execute(
                "SELECT extname FROM pg_extension"
            ).fetchall()
            installed_exts = {r[0] for r in ext_rows}
            if not installed_exts.issubset(ALLOWED_PREINSTALLED_EXTENSIONS):
                raise PostgresBackupError("target_not_empty")
    except PostgresBackupError:
        raise
    except psycopg.Error:
        raise PostgresBackupError("target_connection_failed") from None


def _count_restored_user_objects(
    endpoint: _PgEndpoint, limits: BackupLimits
) -> tuple[int, int]:
    try:
        with psycopg.connect(**endpoint.connect_kwargs(limits)) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
            if not row or row[0] != endpoint.database:
                raise PostgresBackupError("target_endpoint_mismatch")

            table_count = conn.execute(
                """
                SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
                  AND c.relkind IN ('r', 'p')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid
                        AND d.deptype = 'e'
                  )
                """
            ).fetchone()[0]

            object_count = conn.execute(
                """
                SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'i')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid
                        AND d.deptype = 'e'
                  )
                """
            ).fetchone()[0]
            return int(table_count), int(object_count)
    except PostgresBackupError:
        raise
    except psycopg.Error:
        raise PostgresBackupError("post_restore_check_failed") from None


def create_postgres_backup(
    source_database_url: str,
    backup_dir: Path,
    *,
    limits: BackupLimits | None = None,
    pg_dump_bin: str | Path = "pg_dump",
) -> BackupManifest:
    """Create a verified pg_dump custom-format archive and integrity manifest.

    ``backup_dir`` must not already exist; its parent directory must be an
    existing non-symlink directory. On any failure, ``backup_dir`` is removed
    best-effort so partial archives are never left behind.
    """
    active_limits = limits if limits is not None else BackupLimits()
    if not isinstance(active_limits, BackupLimits):
        raise PostgresBackupError("invalid_limits")

    endpoint = _parse_pg_url(source_database_url)
    target_dir = _validate_clean_absolute_path(backup_dir)
    if len(target_dir.parts) < 2:
        raise PostgresBackupError("unsafe_path")

    dump_exec = _resolve_executable(pg_dump_bin)

    parent_fd = _open_dir_nofollow(target_dir.parent)
    dir_created = False
    dir_fd = -1
    archive_fd = -1
    manifest_fd = -1
    try:
        _check_source_reachable(endpoint, active_limits)

        try:
            os.mkdir(target_dir.name, 0o700, dir_fd=parent_fd)
            dir_created = True
        except FileExistsError:
            raise PostgresBackupError("destination_exists") from None
        except OSError:
            raise PostgresBackupError("backup_io_failed") from None

        dir_fd = os.open(
            target_dir.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        archive_fd = os.open(
            ARCHIVE_FILENAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )

        argv = [
            dump_exec,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            "--encoding=UTF8",
        ]
        _run_bounded_command(
            argv,
            env=endpoint.subprocess_env(active_limits),
            limits=active_limits,
            stdout_fd=archive_fd,
            max_stdout_bytes=active_limits.max_archive_bytes,
        )
        os.fsync(archive_fd)

        archive_size, digest, header = _stream_verify_archive_fd(
            archive_fd, active_limits.max_archive_bytes
        )
        os.close(archive_fd)
        archive_fd = -1

        if archive_size < len(PGDMP_MAGIC) or not header.startswith(PGDMP_MAGIC):
            raise PostgresBackupError("invalid_archive_header")

        manifest = BackupManifest(
            format=MANIFEST_FORMAT,
            archive_filename=ARCHIVE_FILENAME,
            archive_sha256=digest,
            archive_size_bytes=archive_size,
            source_endpoint_sha256=endpoint.endpoint_fingerprint,
            source_database_sha256=endpoint.database_fingerprint,
        )
        manifest_payload = json.dumps(
            manifest.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        if len(manifest_payload) > MAX_MANIFEST_BYTES:
            raise PostgresBackupError("size_limit_exceeded")

        manifest_fd = os.open(
            MANIFEST_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        with os.fdopen(os.dup(manifest_fd), "wb") as stream:
            stream.write(manifest_payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(manifest_fd)
        manifest_fd = -1
        os.fsync(dir_fd)
        return manifest
    except BaseException:
        if archive_fd >= 0:
            try:
                os.close(archive_fd)
            except OSError:
                pass
            archive_fd = -1
        if manifest_fd >= 0:
            try:
                os.close(manifest_fd)
            except OSError:
                pass
            manifest_fd = -1
        if dir_fd >= 0 and dir_created:
            # Only remove our known files through the original directory handle.
            # Never recursively delete a pathname that another process could swap.
            for name in (ARCHIVE_FILENAME, MANIFEST_FILENAME):
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except OSError:
                    pass
            try:
                opened = os.fstat(dir_fd)
                current = os.stat(target_dir.name, dir_fd=parent_fd, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                    os.rmdir(target_dir.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        for fd in (archive_fd, manifest_fd, dir_fd, parent_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _open_and_verify_backup_archive(
    backup_dir: Path,
    active_limits: BackupLimits,
) -> tuple[BackupManifest, int, tuple[int, int]]:
    """Verify backup directory and return ``(manifest, open_archive_fd, (st_dev, st_ino))``.

    The caller owns ``open_archive_fd`` and must close it. Keeping the verified
    descriptor open prevents pathname replacement or symlink swaps between
    verification and ``pg_restore``.
    """
    target_dir = _validate_clean_absolute_path(backup_dir)
    dir_fd = _open_dir_nofollow(target_dir)
    archive_fd = -1
    try:
        entries = set(os.listdir(dir_fd))
        if entries != {ARCHIVE_FILENAME, MANIFEST_FILENAME}:
            raise PostgresBackupError("invalid_backup_directory")

        raw_manifest = _read_regular_file_at(dir_fd, MANIFEST_FILENAME, MAX_MANIFEST_BYTES)
        try:
            doc = json.loads(raw_manifest, object_pairs_hook=_unique_json_pairs)
        except PostgresBackupError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise PostgresBackupError("invalid_manifest") from None

        expected_keys = {
            "format",
            "archive_filename",
            "archive_sha256",
            "archive_size_bytes",
            "source_endpoint_sha256",
            "source_database_sha256",
        }
        if not isinstance(doc, dict) or set(doc) != expected_keys:
            raise PostgresBackupError("invalid_manifest")
        if doc["format"] != MANIFEST_FORMAT or doc["archive_filename"] != ARCHIVE_FILENAME:
            raise PostgresBackupError("invalid_manifest")

        archive_sha256 = doc["archive_sha256"]
        source_ep_sha256 = doc["source_endpoint_sha256"]
        source_db_sha256 = doc["source_database_sha256"]
        archive_size = doc["archive_size_bytes"]
        if (
            not isinstance(archive_sha256, str)
            or not _SHA256_RE.fullmatch(archive_sha256)
            or not isinstance(source_ep_sha256, str)
            or not _SHA256_RE.fullmatch(source_ep_sha256)
            or not isinstance(source_db_sha256, str)
            or not _SHA256_RE.fullmatch(source_db_sha256)
            or type(archive_size) is not int
            or archive_size < len(PGDMP_MAGIC)
            or archive_size > active_limits.max_archive_bytes
        ):
            raise PostgresBackupError("invalid_manifest")

        try:
            archive_fd = os.open(
                ARCHIVE_FILENAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=dir_fd,
            )
        except OSError:
            raise PostgresBackupError("unsafe_path") from None

        actual_size, actual_sha256, header = _stream_verify_archive_fd(
            archive_fd, active_limits.max_archive_bytes
        )
        if actual_size != archive_size:
            raise PostgresBackupError("archive_size_mismatch")
        if not header.startswith(PGDMP_MAGIC):
            raise PostgresBackupError("invalid_archive_header")
        if actual_sha256 != archive_sha256:
            raise PostgresBackupError("archive_digest_mismatch")

        st = os.fstat(archive_fd)
        manifest = BackupManifest(
            format=MANIFEST_FORMAT,
            archive_filename=ARCHIVE_FILENAME,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size,
            source_endpoint_sha256=source_ep_sha256,
            source_database_sha256=source_db_sha256,
        )
        keep_fd = archive_fd
        archive_fd = -1
        return manifest, keep_fd, (st.st_dev, st.st_ino)
    finally:
        if archive_fd >= 0:
            try:
                os.close(archive_fd)
            except OSError:
                pass
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _stage_verified_archive_fd(archive_fd: int, expected_size: int, expected_sha256: str) -> int:
    """Copy a verified archive descriptor into a private unlinked temporary file descriptor.

    Staging into an unlinked on-disk file descriptor avoids holding a multi-GB
    archive in RAM while ensuring that even if another local process mutates,
    unlinks, or symlink-swaps ``<backup_dir>/database.pgdump`` after
    verification, ``pg_restore`` reads only the exact immutable verified bytes
    from its private anonymous file descriptor via ``stdin``.
    """
    staged_fd = -1
    try:
        tmp_dir = tempfile.gettempdir()
        try:
            staged_fd = os.open(
                tmp_dir,
                os.O_RDWR | getattr(os, "O_TMPFILE", 0o20200000) | os.O_EXCL,
                0o600,
            )
        except OSError:
            fd, tmp_path = tempfile.mkstemp(prefix=".lab-pg-archive-", dir=tmp_dir)
            try:
                os.unlink(tmp_path)
            except OSError:
                os.close(fd)
                raise
            staged_fd = fd

        os.lseek(archive_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        header = bytearray()
        while True:
            chunk = os.read(archive_fd, STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if len(header) < len(PGDMP_MAGIC):
                header.extend(chunk[: len(PGDMP_MAGIC) - len(header)])
            total += len(chunk)
            if total > expected_size:
                raise PostgresBackupError("archive_size_mismatch")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(staged_fd, view)
                view = view[written:]

        if total != expected_size:
            raise PostgresBackupError("archive_size_mismatch")
        if not bytes(header).startswith(PGDMP_MAGIC):
            raise PostgresBackupError("invalid_archive_header")
        if digest.hexdigest() != expected_sha256:
            raise PostgresBackupError("archive_digest_mismatch")

        os.lseek(staged_fd, 0, os.SEEK_SET)
        keep = staged_fd
        staged_fd = -1
        return keep
    except PostgresBackupError:
        raise
    except OSError:
        raise PostgresBackupError("backup_io_failed") from None
    finally:
        if staged_fd >= 0:
            try:
                os.close(staged_fd)
            except OSError:
                pass


def verify_backup_directory(
    backup_dir: Path,
    *,
    limits: BackupLimits | None = None,
) -> BackupManifest:
    """Verify backup directory layout, manifest schema, archive size, header, and SHA-256."""
    active_limits = limits if limits is not None else BackupLimits()
    if not isinstance(active_limits, BackupLimits):
        raise PostgresBackupError("invalid_limits")

    manifest, archive_fd, _ = _open_and_verify_backup_archive(backup_dir, active_limits)
    try:
        return manifest
    finally:
        try:
            os.close(archive_fd)
        except OSError:
            pass


def verify_postgres_restore(
    backup_dir: Path,
    target_database_url: str,
    *,
    confirm_disposable_target_database: str,
    source_database_url: str | None = None,
    limits: BackupLimits | None = None,
    pg_restore_bin: str | Path = "pg_restore",
) -> RestoreVerificationReport:
    """Verify archive integrity and restore into an explicitly identified disposable empty DB.

    This function never creates or drops a database, refuses production/non-disposable
    database names, refuses targets whose database name or endpoint matches the
    source, stages the verified archive into a private unlinked file descriptor
    so ``pg_restore`` never reopens a mutable pathname on disk, and fails closed
    if the target database is non-empty or in use before restore.
    """
    active_limits = limits if limits is not None else BackupLimits()
    if not isinstance(active_limits, BackupLimits):
        raise PostgresBackupError("invalid_limits")

    target_endpoint = _parse_pg_url(target_database_url)
    source_endpoint = (
        _parse_pg_url(source_database_url) if source_database_url is not None else None
    )
    _validate_disposable_target(
        target_endpoint,
        confirm_disposable_target_database,
        source=source_endpoint,
    )

    manifest, archive_fd, _ = _open_and_verify_backup_archive(backup_dir, active_limits)
    staged_fd = -1
    try:
        if source_endpoint is not None and (
            manifest.source_endpoint_sha256 != source_endpoint.endpoint_fingerprint
            or manifest.source_database_sha256 != source_endpoint.database_fingerprint
        ):
            raise PostgresBackupError("source_endpoint_mismatch")
        _validate_disposable_target(
            target_endpoint,
            confirm_disposable_target_database,
            source=source_endpoint,
            manifest=manifest,
        )

        # Copy the verified archive descriptor into an unlinked anonymous file
        # descriptor and close the on-disk descriptor so subsequent pathname
        # replacements, symlink swaps, or in-place file mutations cannot affect
        # either pg_restore --list or the restore itself.
        staged_fd = _stage_verified_archive_fd(
            archive_fd, manifest.archive_size_bytes, manifest.archive_sha256
        )
        os.close(archive_fd)
        archive_fd = -1

        restore_exec = _resolve_executable(pg_restore_bin)
        _verify_target_database_empty(target_endpoint, active_limits)

        # 1. Inspect archive TOC via pg_restore --list reading from stdin (staged_fd)
        # without any filesystem path argument.
        os.lseek(staged_fd, 0, os.SEEK_SET)
        toc_out = _run_bounded_command(
            [restore_exec, "--list", "--format=custom"],
            env=target_endpoint.subprocess_env(active_limits),
            limits=active_limits,
            stdin_fd=staged_fd,
        )
        toc_lines = [
            line
            for line in toc_out.decode("utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        ]

        # 2. Rewind the private unlinked staged descriptor and restore from stdin
        # with --exit-on-error and --single-transaction.
        os.lseek(staged_fd, 0, os.SEEK_SET)
        _run_bounded_command(
            [
                restore_exec,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--exit-on-error",
                "--single-transaction",
                f"--dbname={target_endpoint.database}",
            ],
            env=target_endpoint.subprocess_env(active_limits),
            limits=active_limits,
            stdin_fd=staged_fd,
        )
    finally:
        for fd in (archive_fd, staged_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    table_count, object_count = _count_restored_user_objects(
        target_endpoint, active_limits
    )
    return RestoreVerificationReport(
        archive_sha256=manifest.archive_sha256,
        archive_size_bytes=manifest.archive_size_bytes,
        restored_catalog_entry_count=len(toc_lines),
        restored_user_table_count=table_count,
        restored_user_object_count=object_count,
    )
