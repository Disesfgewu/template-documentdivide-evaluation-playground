"""Executable bounded CI runtime smoke test for Docker runtime images.

Validates deploy/api.Dockerfile and deploy/parser.Dockerfile against real PostgreSQL:
1. Builds both Docker runtime images with locked dependencies and non-root users.
2. Starts containers with CPU, memory, and pids limits, read-only root, and tmpfs.
3. Asserts /healthz, /readyz, and /v1/capabilities endpoints.
4. Verifies non-root UID runtime (UID 10001 != 0).
5. Exercises authenticated synthetic PDF upload and byte-identical download.
6. Enforces cross-project authorization denial (404).
7. Proves PDF native library availability (pypdfium2/PDFium page extraction).
8. Proves PostgreSQL transactional persistence and host volume artifact persistence.
9. Ensures failure-safe cleanup of containers, disposable DB, and temporary volume.
10. Writes a structured measured-results report without disclosing credentials or tokens.

Refs #3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Configure Python paths for repo packages
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts", _REPO_ROOT / "fixtures"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import psycopg
from psycopg.rows import dict_row

from fixtures.generators import synthetic_pdf
from lab_api.auth import issue_actor_token
from lab_db.migrate import migrate


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.read(), resp_headers
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, exc.read(), resp_headers


def _http_post(url: str, data: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.read(), resp_headers
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, exc.read(), resp_headers


def _wait_for_ready(url: str, timeout_s: float = 30.0) -> dict:
    start = time.monotonic()
    last_exc: Exception | None = None
    while time.monotonic() - start < timeout_s:
        try:
            status, body, _ = _http_get(url)
            if status == 200:
                return json.loads(body.decode("utf-8"))
        except Exception as exc:
            last_exc = exc
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url} to become ready; last error: {last_exc}")


def _run_docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def run_smoke(admin_database_url: str, report_dir: Path) -> dict:
    if shutil.which("docker") is None:
        print("BLOCKED: docker CLI not found on host (local host Docker unavailable)", file=sys.stderr)
        sys.exit(3)

    smoke_id = uuid.uuid4().hex[:8]
    smoke_dbname = f"lab_smoke_{smoke_id}"
    smoke_db_url = _with_db(admin_database_url, smoke_dbname)
    api_container_name = f"lab-api-smoke-{smoke_id}"
    parser_container_name = f"lab-parser-smoke-{smoke_id}"
    auth_secret = f"ci-smoke-secret-{smoke_id}"

    artifact_dir = tempfile.mkdtemp(prefix=f"lab-smoke-artifacts-{smoke_id}-")
    os.chmod(artifact_dir, 0o777)

    measured: dict[str, object] = {
        "smoke_id": smoke_id,
        "timestamp_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    t0 = time.monotonic()

    # Track resources for guaranteed cleanup
    created_db = False
    containers_started = []

    try:
        # 1. Disposable PostgreSQL database and schema migration
        print(f"[smoke] Creating disposable database {smoke_dbname}...")
        with psycopg.connect(admin_database_url, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{smoke_dbname}"')
            created_db = True
        with psycopg.connect(smoke_db_url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

        print("[smoke] Applying migrations via lab_db.migrate...")
        applied_migs = migrate(smoke_db_url, deployment="ci_smoke")
        print(f"[smoke] Applied {len(applied_migs)} migrations.")
        measured["migrations_applied_count"] = len(applied_migs)

        # 2. Seed test projects and actors
        proj_a = uuid.uuid4()
        owner_a = uuid.uuid4()
        proj_b = uuid.uuid4()
        owner_b = uuid.uuid4()

        with psycopg.connect(smoke_db_url) as conn:
            with conn.transaction():
                conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (proj_a, "smoke-proj-a"))
                conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'owner-a')", (owner_a,))
                conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (proj_a, owner_a))

                conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (proj_b, "smoke-proj-b"))
                conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'owner-b')", (owner_b,))
                conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (proj_b, owner_b))

        token_a = issue_actor_token(owner_a, auth_secret)
        token_b = issue_actor_token(owner_b, auth_secret)

        # 3. Build Docker runtime images
        print("[smoke] Building deploy/api.Dockerfile...")
        _run_docker(["build", "-f", "deploy/api.Dockerfile", "-t", f"lab-api:smoke-{smoke_id}", "."])
        print("[smoke] Building deploy/parser.Dockerfile...")
        _run_docker(["build", "-f", "deploy/parser.Dockerfile", "-t", f"lab-parser:smoke-{smoke_id}", "."])
        measured["docker_builds_succeeded"] = True

        # 4. Start containers with CPU/memory/pids limits + read-only root + tmpfs
        print("[smoke] Starting containers with resource limits and read-only root...")
        api_run = _run_docker([
            "run", "-d",
            "--name", api_container_name,
            "--network", "host",
            "--cpus=1.0",
            "--memory=512m",
            "--pids-limit=100",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-v", f"{artifact_dir}:/var/lib/lab/artifacts",
            "-e", "LAB_ENV=test",
            "-e", "LAB_PROVIDER_MODE=mock",
            "-e", f"LAB_DATABASE_URL={smoke_db_url}",
            "-e", "LAB_ARTIFACT_ROOT=/var/lib/lab/artifacts",
            "-e", f"LAB_AUTH_SECRET={auth_secret}",
            f"lab-api:smoke-{smoke_id}",
        ])
        containers_started.append(api_container_name)
        measured["api_container_id"] = api_run.stdout.strip()[:12]

        parser_run = _run_docker([
            "run", "-d",
            "--name", parser_container_name,
            "--network", "host",
            "--cpus=1.0",
            "--memory=512m",
            "--pids-limit=100",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-v", f"{artifact_dir}:/var/lib/lab/artifacts",
            "-e", "LAB_PARSER_ENV=test",
            "-e", "LAB_PARSER_ARTIFACT_ROOT=/var/lib/lab/artifacts",
            f"lab-parser:smoke-{smoke_id}",
        ])
        containers_started.append(parser_container_name)
        measured["parser_container_id"] = parser_run.stdout.strip()[:12]

        # 5. Verify non-root user UID in both containers
        api_uid = _run_docker(["exec", api_container_name, "id", "-u"]).stdout.strip()
        parser_uid = _run_docker(["exec", parser_container_name, "id", "-u"]).stdout.strip()
        print(f"[smoke] Verified non-root UIDs: API={api_uid}, Parser={parser_uid}")
        if api_uid != "10001" or parser_uid != "10001":
            raise AssertionError(f"Expected UID 10001 for non-root containers, got API={api_uid}, Parser={parser_uid}")
        measured["nonroot_runtime_verified"] = {"api_uid": int(api_uid), "parser_uid": int(parser_uid)}

        # 6. Wait for health and readiness
        print("[smoke] Waiting for services to become ready...")
        api_ready = _wait_for_ready("http://127.0.0.1:8000/readyz", timeout_s=30.0)
        parser_ready = _wait_for_ready("http://127.0.0.1:8100/readyz", timeout_s=30.0)

        health_api_status, health_api_body, _ = _http_get("http://127.0.0.1:8000/healthz")
        health_parser_status, health_parser_body, _ = _http_get("http://127.0.0.1:8100/healthz")

        if health_api_status != 200 or health_parser_status != 200:
            raise AssertionError(f"Health check failed: API={health_api_status}, Parser={health_parser_status}")

        measured["health_status"] = {"api": health_api_status, "parser": health_parser_status}
        measured["readiness_status"] = {
            "api": api_ready.get("status"),
            "parser": parser_ready.get("status"),
            "api_database_check": api_ready.get("checks", {}).get("database", {}).get("ok"),
            "api_storage_check": api_ready.get("checks", {}).get("artifact_storage", {}).get("ok"),
            "parser_storage_check": parser_ready.get("checks", {}).get("artifact_storage", {}).get("ok"),
        }
        print(f"[smoke] Services ready: API={api_ready.get('status')}, Parser={parser_ready.get('status')}")

        # 7. Assert parser capabilities endpoint
        caps_status, caps_body, _ = _http_get("http://127.0.0.1:8100/v1/capabilities")
        if caps_status != 200:
            raise AssertionError(f"Parser /v1/capabilities failed with status {caps_status}")
        caps_data = json.loads(caps_body.decode("utf-8"))
        if caps_data.get("service_version") != "0.1.0" or not isinstance(caps_data.get("parsers"), list):
            raise AssertionError(f"Unexpected parser capabilities payload: {caps_data}")
        measured["parser_capabilities"] = {"status": caps_status, "service_version": caps_data.get("service_version")}

        # 8. Exercise authenticated synthetic PDF upload
        print("[smoke] Exercising authenticated synthetic PDF upload...")
        raw_pdf = synthetic_pdf.build(3)
        pdf_sha256 = hashlib.sha256(raw_pdf).hexdigest()
        pdf_size = len(raw_pdf)

        upload_url = f"http://127.0.0.1:8000/api/v1/documents?project_id={proj_a}&logical_id=smoke-pdf-3p"
        upload_status, upload_body, _ = _http_post(
            upload_url,
            data=raw_pdf,
            headers={
                "Authorization": f"Bearer {token_a}",
                "Content-Type": "application/pdf",
            },
        )
        if upload_status != 201:
            raise AssertionError(f"PDF upload failed with status {upload_status}: {upload_body.decode('utf-8', errors='replace')}")

        upload_data = json.loads(upload_body.decode("utf-8"))
        doc_id = upload_data["id"]
        if upload_data["sha256"] != pdf_sha256 or upload_data["size_bytes"] != pdf_size or upload_data["page_count"] != 3:
            raise AssertionError(f"Uploaded PDF properties mismatch: {upload_data}")
        print(f"[smoke] Upload successful: doc_id={doc_id}, pages=3, sha256={pdf_sha256[:12]}...")

        # 9. Exercise byte-identical download
        print("[smoke] Exercising byte-identical original PDF download...")
        download_url = f"http://127.0.0.1:8000/api/v1/documents/{doc_id}/original"
        download_status, downloaded_bytes, dl_headers = _http_get(
            download_url,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        if download_status != 200:
            raise AssertionError(f"PDF download failed with status {download_status}")
        if downloaded_bytes != raw_pdf:
            raise AssertionError("Downloaded bytes do not match uploaded synthetic PDF bytes")
        if hashlib.sha256(downloaded_bytes).hexdigest() != pdf_sha256:
            raise AssertionError("Downloaded PDF SHA-256 does not match original SHA-256")
        measured["pdf_upload_download"] = {
            "upload_status": upload_status,
            "download_status": download_status,
            "page_count": upload_data["page_count"],
            "size_bytes": pdf_size,
            "sha256_match": True,
            "byte_identical": True,
        }

        # 10. Exercise cross-project denied request (actor from project B accessing project A doc)
        print("[smoke] Asserting cross-project authorization boundary...")
        cross_status, cross_body, _ = _http_get(
            download_url,
            headers={"Authorization": f"Bearer {token_b}"},
        )
        if cross_status != 404:
            raise AssertionError(f"Expected cross-project request to return 404, got {cross_status}")
        measured["cross_project_denial_status"] = cross_status

        # 11. Verify PostgreSQL database state and native page extraction proof
        print("[smoke] Verifying PostgreSQL database state and native page extraction...")
        with psycopg.connect(smoke_db_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM document_versions WHERE id = %s", (uuid.UUID(doc_id),))
                doc_row = cur.fetchone()
                if not doc_row or doc_row["sha256"] != pdf_sha256 or doc_row["page_count"] != 3:
                    raise AssertionError(f"PostgreSQL document_version invalid: {doc_row}")

                cur.execute("SELECT * FROM artifacts WHERE id = %s", (doc_row["artifact_id"],))
                art_row = cur.fetchone()
                if not art_row or art_row["sha256"] != pdf_sha256 or art_row["state"] != "committed":
                    raise AssertionError(f"PostgreSQL artifact row invalid: {art_row}")

                cur.execute("SELECT count(*) AS cnt FROM pages WHERE document_version_id = %s", (uuid.UUID(doc_id),))
                page_cnt = cur.fetchone()["cnt"]
                if page_cnt != 3:
                    raise AssertionError(f"Expected 3 extracted page geometries, got {page_cnt}")

        measured["db_persistence_verified"] = {
            "document_version_persisted": True,
            "artifact_committed": True,
            "extracted_pages_count": page_cnt,
        }

        # 12. Verify host volume artifact file persistence
        print("[smoke] Verifying host volume artifact file persistence...")
        artifact_path = Path(artifact_dir) / "sha256" / pdf_sha256[:2] / pdf_sha256
        if not artifact_path.is_file():
            raise AssertionError(f"Artifact file not found at expected path: {artifact_path}")
        persisted_bytes = artifact_path.read_bytes()
        if persisted_bytes != raw_pdf:
            raise AssertionError("Persisted artifact file bytes do not match original PDF bytes")
        measured["host_volume_artifact_persisted"] = {
            "file_exists": True,
            "size_bytes": len(persisted_bytes),
            "sha256_match": True,
        }

        measured["status"] = "PASSED"
        measured["duration_s"] = round(time.monotonic() - t0, 3)
        print(f"[smoke] SUCCESS: All runtime smoke assertions passed in {measured['duration_s']}s.")

    finally:
        print("[smoke] Cleaning up containers and temporary resources...")
        for cname in containers_started:
            _run_docker(["stop", "-t", "5", cname], check=False)
            _run_docker(["rm", "-f", cname], check=False)

        shutil.rmtree(artifact_dir, ignore_errors=True)

        if created_db:
            try:
                with psycopg.connect(admin_database_url, autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{smoke_dbname}" WITH (FORCE)')
            except Exception as exc:
                print(f"[smoke] Warning: Failed to drop temporary database {smoke_dbname}: {exc}", file=sys.stderr)

    # 13. Write measured results report
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = report_dir / "runtime_smoke_report.json"
    report_md_path = report_dir / "runtime_smoke_report.md"

    report_json_path.write_text(json.dumps(measured, indent=2), encoding="utf-8")

    md_content = f"""# Runtime Container Smoke Report

- **Status**: {measured.get('status')}
- **Duration**: {measured.get('duration_s')}s
- **Timestamp**: {measured.get('timestamp_start')}
- **Smoke ID**: {smoke_id}

## Measured Assertions
- **Docker Images Built**: {measured.get('docker_builds_succeeded')}
- **Non-root Runtime UIDs**: API={measured.get('nonroot_runtime_verified', {}).get('api_uid')}, Parser={measured.get('nonroot_runtime_verified', {}).get('parser_uid')}
- **Health Checks**: API={measured.get('health_status', {}).get('api')}, Parser={measured.get('health_status', {}).get('parser')}
- **Readiness Checks**: API={measured.get('readiness_status', {}).get('api')}, Parser={measured.get('readiness_status', {}).get('parser')}
- **Parser Capabilities**: Status={measured.get('parser_capabilities', {}).get('status')}, Version={measured.get('parser_capabilities', {}).get('service_version')}
- **PDF Upload/Download**: Status=201/200, Pages={measured.get('pdf_upload_download', {}).get('page_count')}, SHA-256 match={measured.get('pdf_upload_download', {}).get('sha256_match')}
- **Cross-Project Denial**: Status={measured.get('cross_project_denial_status')} (404)
- **Native PDFium Availability**: Extracted pages count={measured.get('db_persistence_verified', {}).get('extracted_pages_count')}
- **Database & Artifact Persistence**: DB committed={measured.get('db_persistence_verified', {}).get('artifact_committed')}, Volume file verified={measured.get('host_volume_artifact_persisted', {}).get('sha256_match')}
"""
    report_md_path.write_text(md_content, encoding="utf-8")
    print(f"[smoke] Report written to {report_json_path} and {report_md_path}")
    return measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("LAB_TEST_DATABASE_URL", ""),
        help="PostgreSQL URL with privileges to create disposable databases (defaults to LAB_TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--report-dir",
        default=Path(os.environ.get("LAB_SMOKE_REPORT_DIR", "/tmp/smoke-report")),
        type=Path,
        help="Directory to write measured results reports",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        print("BLOCKED: PostgreSQL URL required (--database-url or LAB_TEST_DATABASE_URL)", file=sys.stderr)
        return 3

    try:
        run_smoke(args.database_url, args.report_dir.resolve())
        return 0
    except Exception as exc:
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
