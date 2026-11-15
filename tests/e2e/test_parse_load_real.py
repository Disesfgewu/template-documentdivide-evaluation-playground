"""500 and 600 page load MEASUREMENT through the real consumer (Refs #7).

Synthetic F01 PDFs (repository generator) -> API upload -> queue -> ``process_parse_job`` with the real
batcher and real pinned Unstructured fast -> stored result -> GET page latency. Runs only when
``LAB_LOAD_REAL_UNSTRUCTURED=1`` (CI job ``load-parse``), in its OWN process so that
``getrusage`` maxima belong to this run only. It asserts only that the job succeeded with all pages;
every number it records goes into the report (``LAB_LOAD_REPORT``) as measured, and nothing here
claims a threshold was met. NOT measured: worker concurrency above 1, several jobs at once, log or
vector load, scanned pages, tables, hi_res.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_results import make_persist
from lab_api.parse_worker import process_parse_job
from lab_db.storage import LocalArtifactStore
from lab_parser.batch_parse import parse_in_batches
from lab_parser.unstructured_runner import run_unstructured_fast
from fixtures.generators import synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

ENABLED = os.environ.get("LAB_LOAD_REAL_UNSTRUCTURED") == "1"
REPORT: dict = {"synthetic": True, "python": sys.version.split()[0], "cpu_count": os.cpu_count()}


def _children_of(root: int) -> list[int]:
    """Descendant pids of ``root`` from /proc (no psutil dependency)."""
    parent_of = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/stat", "rb") as stream:
                    fields = stream.read().rsplit(b")", 1)[1].split()
                parent_of[int(entry)] = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
    found, frontier = [], [root]
    while frontier:
        current = frontier.pop()
        for pid, parent in parent_of.items():
            if parent == current:
                found.append(pid)
                frontier.append(pid)
    return found


def _rss_kib(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


class RssSampler:
    """Samples the summed and the largest single RSS of this process's descendants every 0.25 s."""

    def __init__(self):
        self.peak_sum = self.peak_single = self.peak_children = 0
        self.self_peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        me = os.getpid()
        while not self._stop.wait(0.25):
            sizes = [_rss_kib(p) for p in _children_of(me)]
            self.samples += 1
            self.peak_children = max(self.peak_children, len(sizes))
            self.peak_sum = max(self.peak_sum, sum(sizes))
            self.peak_single = max(self.peak_single, max(sizes, default=0))
            self.self_peak = max(self.self_peak, _rss_kib(me))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


@unittest.skipUnless(ADMIN_URL and ENABLED, "needs LAB_TEST_DATABASE_URL and LAB_LOAD_REAL_UNSTRUCTURED=1")
class ParseLoadMeasurement(FreshDatabase):
    @classmethod
    def tearDownClass(cls):
        text = json.dumps(REPORT, indent=2, sort_keys=True, default=float)   # SQL sums arrive as Decimal
        print(text)
        path = os.environ.get("LAB_LOAD_REPORT")
        if path:
            Path(path).write_text(text)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
               "LAB_ARTIFACT_ROOT": str(self.root), "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
               "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)
        with self.connect() as c:
            self.project, self.owner = self.make_project(c)

    def headers(self, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(self.owner, self.secret)}", **extra}

    def measure(self, pages: int) -> dict:
        pdf = synthetic_pdf.build(pages)
        up = time.monotonic()
        res = self.client.post(f"/api/v1/documents?project_id={self.project}&logical_id=load{pages}",
                               headers=self.headers(**{"Content-Type": "application/pdf"}), content=pdf)
        upload_seconds = time.monotonic() - up
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(), json={})
        self.assertEqual(res.status_code, 202, res.text)
        job = uuid.UUID(res.json()["job_id"])

        batches, lock = [], threading.Lock()

        def timed(data, **kw):
            began = time.monotonic()
            error = None
            try:
                return run_unstructured_fast(data, **kw)
            except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised for the batcher to handle
                error = getattr(exc, "code", type(exc).__name__)
                raise
            finally:
                with lock:
                    batches.append({"pages": len(kw["page_indices"]), "wall": time.monotonic() - began, "error": error})

        def run(data, **kw):
            return parse_in_batches(data, run_batch=timed, **kw)

        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        with RssSampler() as sampler, self.connect() as conn:
            began = time.monotonic()
            outcome = process_parse_job(conn, self.project, job, store=self.store, persist=make_persist(self.store),
                                        worker="load", max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, run=run,
                                        ttl_seconds=60)
            total = time.monotonic() - began
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))

        with self.connect() as c:
            status, attempt = c.execute("SELECT status::text, attempt FROM jobs WHERE id = %s", (job,)).fetchone()
            sizes = c.execute("SELECT octet_length(result::text), octet_length(manifest::text) FROM parse_results "
                              "WHERE job_id = %s", (job,)).fetchone()
            page_bytes = c.execute("SELECT coalesce(sum(octet_length(evidence::text) + octet_length(observations::text) "
                                   "+ octet_length(diagnostics::text)), 0), count(*), "
                                   "count(*) FILTER (WHERE status = 'ok') FROM parse_page_results WHERE job_id = %s",
                                   (job,)).fetchone()
            artifact_bytes = c.execute("SELECT coalesce(sum(a.size_bytes), 0) FROM parse_results r "
                                       "JOIN artifacts a ON a.id = ANY(r.raw_artifact_ids) WHERE r.job_id = %s",
                                       (job,)).fetchone()[0]
        self.assertEqual(page_bytes[1], pages)

        latencies = []
        for index in [0, pages // 2, pages - 1] * 7:
            began = time.monotonic()
            res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}/pages/{index}", headers=self.headers())
            latencies.append(time.monotonic() - began)
            self.assertEqual(res.status_code, 200)
        began = time.monotonic()
        summary = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers())
        summary_seconds = time.monotonic() - began
        self.assertEqual(summary.status_code, 200)

        walls = sorted(b["wall"] for b in batches if b["pages"] > 1)
        retries = [b for b in batches if b["pages"] == 1]
        return {
            "pages": pages, "pdf_bytes": len(pdf), "upload_seconds": round(upload_seconds, 2),
            "job_status": status, "job_attempts": attempt, "total_wall_seconds": round(total, 2),
            "batches": {"count": len(walls), "median_wall": round(statistics.median(walls), 2) if walls else None,
                        "max_wall": round(max(walls), 2) if walls else None,
                        "min_wall": round(min(walls), 2) if walls else None},
            "single_page_retries": len(retries), "failed_children": [b for b in batches if b["error"]],
            "pages_ok": page_bytes[2],
            "rss_kib": {"children_peak_sum": sampler.peak_sum, "children_peak_single": sampler.peak_single,
                        "children_peak_count": sampler.peak_children, "worker_process_peak": sampler.self_peak,
                        "getrusage_children_max_before": before,
                        "getrusage_children_max_after": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                        "getrusage_self_max": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "samples": sampler.samples},
            "stored": {"result_json_bytes": sizes[0], "manifest_json_bytes": sizes[1], "page_rows_json_bytes": page_bytes[0],
                       "raw_artifact_bytes": artifact_bytes,
                       "record_bytes_approx": sizes[0] + sizes[1] + page_bytes[0],
                       "cap_bytes": 64 * 1024 * 1024},
            "get_page_latency_seconds": {"median": round(statistics.median(latencies), 4),
                                         "max": round(max(latencies), 4), "requests": len(latencies)},
            "get_summary_seconds": round(summary_seconds, 4),
        }

    def test_500_pages(self):
        REPORT["pages_500"] = self.measure(500)

    def test_600_pages(self):
        REPORT["pages_600"] = self.measure(600)


if __name__ == "__main__":
    unittest.main()
