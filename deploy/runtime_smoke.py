"""Executable bounded CI runtime smoke test for Docker runtime images.

Validates deploy/api.Dockerfile and deploy/parser.Dockerfile against real PostgreSQL:
1. Builds both Docker runtime images with locked dependencies and non-root users.
2. Starts containers with CPU, memory, and pids limits, read-only root, and tmpfs.
3. Enforces --cap-drop=ALL, --security-opt=no-new-privileges, and non-world-writable 0770 mount.
4. Asserts /healthz, /readyz (all boolean checks true), and /v1/capabilities endpoints.
5. Verifies live inspect projections (UID 10001, ReadonlyRootfs, memory limit, pids limit).
6. Exercises authenticated synthetic PDF upload and byte-identical download.
7. Executes real PDFium rendering via docker exec inside BOTH containers using lab_parser.render.
8. Proves cross-project authorization denial (404).
9. Proves artifact persistence across container restart (restart -> ready -> byte-identical download).
10. Validates PostgreSQL transactions and host volume artifact file match.
11. Safe failure reporting: always saves measured report in finally without leaking credentials.
12. Failure-safe cleanup with bounded subprocess calls and timeout protection.

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

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts", _REPO_ROOT / "fixtures"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import psycopg
from psycopg.rows import dict_row

from fixtures.generators import synthetic_pdf
from lab_api.auth import issue_actor_token
from lab_db.migrate import migrate

MAX_HTTP_BYTES = 1024 * 1024  # 1 MiB upper bound for synthetic test fixture bodies
TIMEOUT_BUILD_S = 180
TIMEOUT_CMD_S = 30
TIMEOUT_CLEANUP_S = 30
BASE_IMAGE_DIGEST = "python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _http_get(url: str, headers: dict[str, str] | None = None, timeout_s: float = 10.0) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(MAX_HTTP_BYTES + 1)
            if len(body) > MAX_HTTP_BYTES:
                raise ValueError("HTTP response body exceeded 1MiB limit")
            return resp.status, body, resp_headers
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        body = exc.read(MAX_HTTP_BYTES + 1)
        return exc.code, body, resp_headers


def _http_post(url: str, data: bytes, headers: dict[str, str] | None = None, timeout_s: float = 10.0) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(MAX_HTTP_BYTES + 1)
            if len(body) > MAX_HTTP_BYTES:
                raise ValueError("HTTP response body exceeded 1MiB limit")
            return resp.status, body, resp_headers
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        body = exc.read(MAX_HTTP_BYTES + 1)
        return exc.code, body, resp_headers


def _wait_for_ready(url: str, timeout_s: float = 30.0) -> dict:
    start = time.monotonic()
    last_err = "not_started"
    while time.monotonic() - start < timeout_s:
        try:
            status, body, _ = _http_get(url, timeout_s=3.0)
            if status == 200:
                return json.loads(body.decode("utf-8"))
            last_err = f"status_{status}"
        except Exception as exc:
            last_err = type(exc).__name__
        time.sleep(0.5)
    raise TimeoutError(f"readiness timeout for {url} ({last_err})")


def _run_docker(args: list[str], timeout_s: float = TIMEOUT_CMD_S, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_bytes,
            check=check,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(_REPO_ROOT),
        )
    except subprocess.CalledProcessError as exc:
        # Do not leak command args or raw output into message
        raise RuntimeError(f"docker command failed with exit code {exc.returncode}") from None
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"docker command timed out after {timeout_s}s") from None


def _get_git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, cwd=str(_REPO_ROOT))
        return out.stdout.strip()
    except Exception:
        return "unknown"


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

    # Mode 0770 owned by host runner; container receives supplemental group via --group-add
    artifact_dir = tempfile.mkdtemp(prefix=f"lab-smoke-artifacts-{smoke_id}-")
    os.chmod(artifact_dir, 0o770)
    host_gid = os.getgid()

    t0 = time.monotonic()
    start_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    measured: dict[str, object] = {
        "smoke_id": smoke_id,
        "commit_sha": _get_git_commit(),
        "base_image_digest": BASE_IMAGE_DIGEST,
        "python_version": sys.version.split()[0],
        "timestamp_start": start_time_iso,
        "status": "FAILED",
        "completed_assertions": [],
        "measured_data": {},
        "cleanup_status": {},
    }

    created_db = False
    registered_containers = [api_container_name, parser_container_name]
    db_options = "-c statement_timeout=30s -c lock_timeout=5s"

    try:
        # 1. Disposable PostgreSQL database and schema migration
        with psycopg.connect(admin_database_url, connect_timeout=5, options=db_options, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{smoke_dbname}"')
            created_db = True
        with psycopg.connect(smoke_db_url, connect_timeout=5, options=db_options, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

        old_pgopts = os.environ.get("PGOPTIONS")
        try:
            os.environ["PGOPTIONS"] = db_options
            applied_migs = migrate(smoke_db_url, deployment="ci_smoke")
        finally:
            if old_pgopts is not None:
                os.environ["PGOPTIONS"] = old_pgopts
            else:
                os.environ.pop("PGOPTIONS", None)

        measured["completed_assertions"].append("database_migrated")
        measured["measured_data"]["migrations_applied_count"] = len(applied_migs)

        # 2. Seed test projects and actors
        proj_a = uuid.uuid4()
        owner_a = uuid.uuid4()
        proj_b = uuid.uuid4()
        owner_b = uuid.uuid4()

        with psycopg.connect(smoke_db_url, connect_timeout=5, options=db_options) as conn:
            with conn.transaction():
                conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (proj_a, "smoke-proj-a"))
                conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'owner-a')", (owner_a,))
                conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (proj_a, owner_a))

                conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (proj_b, "smoke-proj-b"))
                conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'owner-b')", (owner_b,))
                conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (proj_b, owner_b))

        token_a = issue_actor_token(owner_a, auth_secret)
        token_b = issue_actor_token(owner_b, auth_secret)
        measured["completed_assertions"].append("seed_data_created")

        # 3. Build Docker runtime images with 180s timeout
        _run_docker(["build", "-f", "deploy/api.Dockerfile", "-t", f"lab-api:smoke-{smoke_id}", "."], timeout_s=TIMEOUT_BUILD_S)
        _run_docker(["build", "-f", "deploy/parser.Dockerfile", "-t", f"lab-parser:smoke-{smoke_id}", "."], timeout_s=TIMEOUT_BUILD_S)
        measured["completed_assertions"].append("images_built")

        # 4. Start containers with CPU/memory/pids limits + read-only root + tmpfs + supplemental group + dropped caps
        api_run = _run_docker([
            "run", "-d",
            "--name", api_container_name,
            "--network", "host",
            "--cpus=1.0",
            "--memory=512m",
            "--pids-limit=100",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--group-add={host_gid}",
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-v", f"{artifact_dir}:/var/lib/lab/artifacts",
            "-e", "LAB_ENV=test",
            "-e", "LAB_PROVIDER_MODE=mock",
            "-e", f"LAB_DATABASE_URL={smoke_db_url}",
            "-e", "LAB_ARTIFACT_ROOT=/var/lib/lab/artifacts",
            "-e", f"LAB_AUTH_SECRET={auth_secret}",
            f"lab-api:smoke-{smoke_id}",
        ], timeout_s=TIMEOUT_CMD_S)

        parser_run = _run_docker([
            "run", "-d",
            "--name", parser_container_name,
            "--network", "host",
            "--cpus=1.0",
            "--memory=512m",
            "--pids-limit=100",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--group-add={host_gid}",
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-v", f"{artifact_dir}:/var/lib/lab/artifacts",
            "-e", "LAB_PARSER_ENV=test",
            "-e", "LAB_PARSER_ARTIFACT_ROOT=/var/lib/lab/artifacts",
            f"lab-parser:smoke-{smoke_id}",
        ], timeout_s=TIMEOUT_CMD_S)
        measured["completed_assertions"].append("containers_started")

        # 5. Live inspect selective projection (verifying actual runtime limits, user, and readonly root)
        inspect_api_proc = _run_docker(["inspect", "--format", "{{json .Config.User}}|||{{json .HostConfig.ReadonlyRootfs}}|||{{json .HostConfig.Memory}}|||{{json .HostConfig.PidsLimit}}", api_container_name])
        user_val, ro_val, mem_val, pids_val = [json.loads(p) for p in inspect_api_proc.stdout.decode("utf-8").strip().split("|||")]
        if user_val != "10001:10001" or ro_val is not True or mem_val != 536870912 or pids_val != 100:
            raise AssertionError(f"Unexpected inspect projection for API container: user={user_val}, ro={ro_val}, mem={mem_val}, pids={pids_val}")

        inspect_parser_proc = _run_docker(["inspect", "--format", "{{json .Config.User}}|||{{json .HostConfig.ReadonlyRootfs}}|||{{json .HostConfig.Memory}}|||{{json .HostConfig.PidsLimit}}", parser_container_name])
        p_user, p_ro, p_mem, p_pids = [json.loads(p) for p in inspect_parser_proc.stdout.decode("utf-8").strip().split("|||")]
        if p_user != "10001:10001" or p_ro is not True or p_mem != 536870912 or p_pids != 100:
            raise AssertionError(f"Unexpected inspect projection for Parser container: user={p_user}, ro={p_ro}, mem={p_mem}, pids={p_pids}")

        measured["completed_assertions"].append("inspect_projections_verified")
        measured["measured_data"]["runtime_limits"] = {
            "api_user": user_val, "api_readonly": ro_val, "api_memory_bytes": mem_val, "api_pids_limit": pids_val,
            "parser_user": p_user, "parser_readonly": p_ro, "parser_memory_bytes": p_mem, "parser_pids_limit": p_pids,
        }

        # 6. Wait for health and readiness, assert all boolean check flags are True
        api_ready = _wait_for_ready("http://127.0.0.1:8000/readyz", timeout_s=30.0)
        parser_ready = _wait_for_ready("http://127.0.0.1:8100/readyz", timeout_s=30.0)

        if api_ready.get("status") != "ready" or api_ready.get("checks", {}).get("database", {}).get("ok") is not True or api_ready.get("checks", {}).get("artifact_storage", {}).get("ok") is not True:
            raise AssertionError(f"API readiness failed: {api_ready}")
        if parser_ready.get("status") != "ready" or parser_ready.get("checks", {}).get("artifact_storage", {}).get("ok") is not True:
            raise AssertionError(f"Parser readiness failed: {parser_ready}")

        health_api_status, _, _ = _http_get("http://127.0.0.1:8000/healthz")
        health_parser_status, _, _ = _http_get("http://127.0.0.1:8100/healthz")
        if health_api_status != 200 or health_parser_status != 200:
            raise AssertionError(f"Health checks failed: API={health_api_status}, Parser={health_parser_status}")

        measured["completed_assertions"].append("health_and_readiness_true")

        # 7. Assert parser capabilities endpoint
        caps_status, caps_body, _ = _http_get("http://127.0.0.1:8100/v1/capabilities")
        if caps_status != 200:
            raise AssertionError(f"Parser /v1/capabilities status {caps_status}")
        caps_data = json.loads(caps_body.decode("utf-8"))
        if caps_data.get("service_version") != "0.1.0" or caps_data.get("parsers") != []:
            raise AssertionError(f"Unexpected capabilities payload: {caps_data}")
        measured["completed_assertions"].append("parser_capabilities_verified")

        # 8. Exercise authenticated synthetic PDF upload
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
            raise AssertionError(f"PDF upload failed with status {upload_status}")

        upload_data = json.loads(upload_body.decode("utf-8"))
        doc_id = upload_data["id"]
        if upload_data["sha256"] != pdf_sha256 or upload_data["size_bytes"] != pdf_size or upload_data["page_count"] != 3:
            raise AssertionError(f"Uploaded PDF properties mismatch: {upload_data}")
        measured["completed_assertions"].append("pdf_upload_succeeded")
        measured["measured_data"]["fixture_sha256"] = pdf_sha256
        measured["measured_data"]["fixture_page_count"] = 3
        measured["measured_data"]["fixture_size_bytes"] = pdf_size

        # 9. Exercise byte-identical download
        download_url = f"http://127.0.0.1:8000/api/v1/documents/{doc_id}/original"
        download_status, downloaded_bytes, _ = _http_get(
            download_url,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        if download_status != 200 or downloaded_bytes != raw_pdf:
            raise AssertionError("Downloaded bytes do not match uploaded synthetic PDF")
        measured["completed_assertions"].append("pdf_download_byte_identical")

        # 10. Native PDFium execution proof in BOTH containers via lab_parser.render.render_region
        render_code = (
            "import sys, json;"
            "from lab_parser.render import render_region;"
            "raw = sys.stdin.buffer.read();"
            "asset = render_region(data=raw, page_index=0, dpi=150);"
            "assert asset.png.startswith(b'\\x89PNG\\r\\n\\x1a\\n');"
            "print(json.dumps({'png_valid': True, 'width': asset.width_px, 'height': asset.height_px, 'sha256': asset.sha256}))"
        )
        api_render = _run_docker(["exec", "-i", api_container_name, "python3", "-c", render_code], input_bytes=raw_pdf, timeout_s=TIMEOUT_CMD_S)
        api_render_data = json.loads(api_render.stdout.decode("utf-8").strip())

        parser_render = _run_docker(["exec", "-i", parser_container_name, "python3", "-c", render_code], input_bytes=raw_pdf, timeout_s=TIMEOUT_CMD_S)
        parser_render_data = json.loads(parser_render.stdout.decode("utf-8").strip())

        if api_render_data["png_valid"] is not True or api_render_data["sha256"] != parser_render_data["sha256"]:
            raise AssertionError("PDFium rendering validation failed between API and Parser containers")

        measured["completed_assertions"].append("native_pdfium_rendered_both_containers")
        measured["measured_data"]["rendered_png"] = {
            "width": api_render_data["width"],
            "height": api_render_data["height"],
            "sha256": api_render_data["sha256"],
        }

        # 11. Cross-project denied request (actor from project B accessing project A document -> 404)
        cross_status, _, _ = _http_get(
            download_url,
            headers={"Authorization": f"Bearer {token_b}"},
        )
        if cross_status != 404:
            raise AssertionError(f"Expected cross-project request to return 404, got {cross_status}")
        measured["completed_assertions"].append("cross_project_denied_404")

        # 12. Artifact survival across container restart (restart -> ready -> download byte-identical)
        _run_docker(["restart", "-t", "5", api_container_name], timeout_s=TIMEOUT_CMD_S)
        api_ready_after_restart = _wait_for_ready("http://127.0.0.1:8000/readyz", timeout_s=30.0)
        if api_ready_after_restart.get("status") != "ready":
            raise AssertionError("API failed to become ready after container restart")

        post_restart_status, post_restart_bytes, _ = _http_get(
            download_url,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        if post_restart_status != 200 or post_restart_bytes != raw_pdf:
            raise AssertionError("Artifact failed to survive container restart")
        measured["completed_assertions"].append("artifact_survived_restart")

        # 13. Verify PostgreSQL committed records and host volume artifact file
        with psycopg.connect(smoke_db_url, connect_timeout=5, options=db_options) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM document_versions WHERE id = %s", (uuid.UUID(doc_id),))
                doc_row = cur.fetchone()
                if not doc_row or doc_row["sha256"] != pdf_sha256:
                    raise AssertionError("PostgreSQL document_version invalid")

                cur.execute("SELECT * FROM artifacts WHERE id = %s", (doc_row["artifact_id"],))
                art_row = cur.fetchone()
                if not art_row or art_row["sha256"] != pdf_sha256 or art_row["state"] != "committed":
                    raise AssertionError("PostgreSQL artifact row invalid")

        artifact_path = Path(artifact_dir) / "sha256" / pdf_sha256[:2] / pdf_sha256
        if not artifact_path.is_file() or artifact_path.read_bytes() != raw_pdf:
            raise AssertionError("Host artifact file does not match uploaded synthetic PDF bytes")

        measured["completed_assertions"].append("db_and_volume_persistence_verified")
        measured["status"] = "PASSED"

    except Exception as exc:
        print(f"[smoke] Failed at step '{measured['completed_assertions'][-1] if measured['completed_assertions'] else 'init'}': {type(exc).__name__}", file=sys.stderr)
        measured["error_step"] = measured["completed_assertions"][-1] if measured["completed_assertions"] else "init"
        measured["error_class"] = type(exc).__name__
        raise
    finally:
        measured["timestamp_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        measured["duration_s"] = round(time.monotonic() - t0, 3)

        # Cleanup: best effort each resource, no exception masking primary failure
        cleanup_failures = []
        for cname in registered_containers:
            try:
                _run_docker(["stop", "-t", "5", cname], timeout_s=TIMEOUT_CLEANUP_S, check=False)
                _run_docker(["rm", "-f", cname], timeout_s=TIMEOUT_CLEANUP_S, check=False)
            except Exception as exc:
                cleanup_failures.append(f"container_{cname}:{type(exc).__name__}")

        try:
            shutil.rmtree(artifact_dir, ignore_errors=True)
        except Exception as exc:
            cleanup_failures.append(f"artifact_dir:{type(exc).__name__}")

        if created_db:
            try:
                with psycopg.connect(admin_database_url, connect_timeout=5, options=db_options, autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{smoke_dbname}" WITH (FORCE)')
            except Exception as exc:
                cleanup_failures.append(f"db:{type(exc).__name__}")

        measured["cleanup_status"] = {
            "succeeded": len(cleanup_failures) == 0,
            "failures": cleanup_failures,
        }

        # Write safe measured report in finally
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "runtime_smoke_report.json").write_text(json.dumps(measured, indent=2), encoding="utf-8")

            md_lines = [
                "# Runtime Container Smoke Report",
                "",
                f"- **Status**: {measured.get('status')}",
                f"- **Duration**: {measured.get('duration_s')}s",
                f"- **Timestamp**: {measured.get('timestamp_start')} -> {measured.get('timestamp_end')}",
                f"- **Commit SHA**: {measured.get('commit_sha')}",
                f"- **Base Image Digest**: {measured.get('base_image_digest')}",
                f"- **Python Version**: {measured.get('python_version')}",
                f"- **Completed Assertions Count**: {len(measured.get('completed_assertions', []))}",
                f"- **Cleanup Succeeded**: {measured.get('cleanup_status', {}).get('succeeded')}",
                "",
                "## Completed Assertions",
            ]
            for a in measured.get("completed_assertions", []):
                md_lines.append(f"- [x] {a}")
            if measured.get("status") != "PASSED":
                md_lines.append(f"\n> [!CAUTION]\n> Smoke failed at step `{measured.get('error_step')}` with `{measured.get('error_class')}`.")

            (report_dir / "runtime_smoke_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        except Exception as report_exc:
            print(f"[smoke] Failed to write report: {type(report_exc).__name__}", file=sys.stderr)

        if cleanup_failures:
            print(f"[smoke] Resource cleanup failures: {', '.join(cleanup_failures)}", file=sys.stderr)

    return measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir",
        default=Path(os.environ.get("LAB_SMOKE_REPORT_DIR", "/tmp/smoke-report")),
        type=Path,
        help="Directory to write measured results reports",
    )
    args = parser.parse_args(argv)

    db_url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    if not db_url:
        print("BLOCKED: LAB_TEST_DATABASE_URL environment variable is required", file=sys.stderr)
        return 3

    try:
        report = run_smoke(db_url, args.report_dir.resolve())
        if report.get("status") == "PASSED" and report.get("cleanup_status", {}).get("succeeded", False):
            return 0
        return 1
    except Exception as exc:
        print(f"SMOKE FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
