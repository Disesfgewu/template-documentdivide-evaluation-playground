"""What the local stack holds, and whether the database and the artifact volume agree (Issue #194).

Run inside the api container by ``scripts/demo.sh status|down|start`` (never imported by the service):
    docker compose ... exec -T api python - < deploy/data_status.py
Prints ``KEY=VALUE`` lines: counts of what a wipe would destroy, and ``DANGLING_ARTIFACTS`` = committed artifact rows whose
object is missing or damaged on the artifact volume (0 after a normal stop/start). Read-only; prints no document content,
no key, no token."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

from lab_api.config import load_settings
from lab_db.storage import LocalArtifactStore

COUNTS = [
    ("PROJECTS", "SELECT count(*) FROM projects"),
    ("DOCUMENTS", "SELECT count(*) FROM document_versions"),
    ("BENCHMARKS", "SELECT count(*) FROM benchmark_revisions"),
    ("FROZEN_BENCHMARKS", "SELECT count(*) FROM benchmark_revisions WHERE status::text = 'frozen'"),
    ("QUESTIONS", "SELECT count(*) FROM questions"),
    ("RUNS", "SELECT count(*) FROM runs"),
    ("DEVELOPER_UPLOADS", "SELECT count(*) FROM external_submissions"),
    ("ARTIFACTS", "SELECT count(*) FROM artifacts WHERE state::text = 'committed'"),
]


def main() -> int:
    settings = load_settings()
    store = LocalArtifactStore(Path(os.environ["LAB_ARTIFACT_ROOT"]))
    with psycopg.connect(settings.database_url.get_secret_value()) as conn:
        for name, sql in COUNTS:
            print(f"{name}={conn.execute(sql).fetchone()[0]}")
        dangling = 0
        for key, size in conn.execute("SELECT object_key, size_bytes FROM artifacts WHERE state::text = 'committed'").fetchall():
            try:
                store.verify(key, expected_size=size)
            except Exception:           # missing, truncated or damaged: the type is enough, never a path or content
                dangling += 1
        print(f"DANGLING_ARTIFACTS={dangling}")
        rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        print(f"MIGRATION={rev[0] if rev else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
