"""Readiness checks. Each check touches the real dependency; none is faked."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psycopg

from .config import Settings


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


Check = Callable[[], CheckResult]


def connect(settings: Settings) -> psycopg.Connection:
    """Open a connection whose connect *and* statements are time-bounded."""
    timeout_s = settings.db_connect_timeout_s
    return psycopg.connect(
        settings.database_url.get_secret_value(),
        connect_timeout=max(1, int(timeout_s)),
        options=f"-c statement_timeout={int(timeout_s * 1000)}",
        autocommit=True,
    )


def database_check(settings: Settings) -> Check:
    def run() -> CheckResult:
        try:
            with connect(settings) as conn:
                row = conn.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                ).fetchone()
        except psycopg.Error as exc:
            # Class name only: driver messages can echo host/user details.
            return CheckResult(False, f"connection failed ({type(exc).__name__})")
        if row is None:
            return CheckResult(False, "connected, but the pgvector extension 'vector' is not installed")
        return CheckResult(True, f"connected; pgvector {row[0]}")

    return run


def artifact_storage_check(root: Path) -> Check:
    def run() -> CheckResult:
        if not root.is_dir():
            return CheckResult(False, "artifact root does not exist or is not a directory")
        try:
            fd, name = tempfile.mkstemp(prefix=".readyz-", dir=root)
            os.close(fd)
            os.unlink(name)
        except OSError as exc:
            return CheckResult(False, f"artifact root is not writable ({type(exc).__name__})")
        return CheckResult(True, "writable")

    return run


def default_checks(settings: Settings) -> dict[str, Check]:
    return {
        "database": database_check(settings),
        "artifact_storage": artifact_storage_check(settings.artifact_root),
    }


def run_checks(checks: dict[str, Check]) -> tuple[bool, dict[str, dict]]:
    results: dict[str, dict] = {}
    for name, check in checks.items():
        try:
            res = check()
        except Exception as exc:  # a crashing check is a failed check, never a pass
            res = CheckResult(False, f"check raised {type(exc).__name__}")
        results[name] = {"ok": res.ok, "detail": res.detail}
    return all(r["ok"] for r in results.values()) and bool(results), results
