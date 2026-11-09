"""Stored parse results and their read API on real PostgreSQL (PM plan item 5, Refs #7).

The batch comes from the real #54 normalizer over SYNTHETIC upstream-shaped elements; no parser
library runs here (that evidence is the CI probe)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from psycopg import errors

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_results import record_from_batch
from lab_db import jobs
from lab_db.parse_results import ParseResultConflict, persist_parse_result
from lab_db.storage import LocalArtifactStore
from lab_parser.normalize import DisplayFrame, UnstructuredParser, UnstructuredProfile
from fixtures.generators import synthetic_pdf
from tests.parser.test_normalize import _make_request, _make_unstructured_manifest

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

PAGES = 3


def element(page, text, eid):
    return {"type": "NarrativeText", "element_id": eid, "text": text, "metadata": {
        "page_number": page, "coordinates": {"system": "PixelSpace", "layout_width": 612, "layout_height": 792,
                                             "points": [[61.2, 79.2], [61.2, 158.4], [306, 158.4], [306, 79.2]]}}}


def make_batch(document_id, operation_id, texts=("Alpha", "Gamma")):
    elements = [element(1, texts[0], "e1"), element(3, texts[1], "e3")]
    request = _make_request([0, 1, 2]).model_copy(update={"operation_id": str(operation_id)})
    manifest = _make_unstructured_manifest([0, 1, 2])
    frames = {p: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0,
                              transform_ref=f"tr-{p}") for p in range(PAGES)}
    return UnstructuredParser().normalize(
        json.dumps(elements).encode(), request, manifest, UnstructuredProfile(strategy="fast"),
        document_id=str(document_id), document_sha256="a" * 64, frames=frames, document_page_count=PAGES)


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ParseResultsRealDbTests(FreshDatabase):
    def setUp(self) -> None:
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

    def headers(self, actor, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}", **extra}

    def seed(self):
        with self.connect() as c:
            project, owner = self.make_project(c)
            observer = self.add_member(c, project, "observer")
            reviewer = self.add_member(c, project, "reviewer")
            _, outsider = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=r{uuid.uuid4().hex[:6]}",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}),
                               content=synthetic_pdf.build(PAGES))
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json={})
        self.assertEqual(res.status_code, 202, res.text)
        job = uuid.UUID(res.json()["job_id"])
        with self.connect() as c:
            op = c.execute("SELECT operation_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()[0]
        return SimpleSeed(project, owner, observer, reviewer, outsider, doc, job, op)

    def finish_with_result(self, s, batch=None, texts=("Alpha", "Gamma")):
        batch = batch or make_batch(s.doc, s.op, texts)
        record = record_from_batch(batch)
        with self.connect() as c:
            lease = jobs.acquire(c, s.project, s.job, "w1")
            jobs.finish(c, s.project, lease, "succeeded", "parsed",
                        hook=lambda conn: persist_parse_result(conn, self.store, s.project, lease, record))
        return batch

    def test_result_and_job_status_commit_together_and_raw_bytes_are_stored(self):
        s = self.seed()
        batch = self.finish_with_result(s)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (s.job,)).fetchone()[0], "succeeded")
            ids, result = c.execute("SELECT raw_artifact_ids, result FROM parse_results WHERE job_id = %s", (s.job,)).fetchone()
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (s.job,)).fetchone()[0], PAGES)
            for ref in result["raw_result_refs"]:
                sha = ref.split(":", 1)[1]
                row = c.execute("SELECT id, state::text, object_key FROM artifacts WHERE project_id = %s AND sha256 = %s",
                                (s.project, sha)).fetchone()
                self.assertIsNotNone(row, ref)
                self.assertIn(row[0], ids)
                self.assertEqual(row[1], "committed")
                self.assertEqual(hashlib.sha256(self.store.open(row[2])).hexdigest(), sha)
        self.assertEqual(result["operation_id"], str(s.op))
        self.assertEqual(len(batch.evidence), 2)

    def test_a_crash_after_persist_leaves_no_result_and_a_running_job(self):
        s = self.seed()
        record = record_from_batch(make_batch(s.doc, s.op))
        with self.connect() as c:
            lease = jobs.acquire(c, s.project, s.job, "w1")

            def hook(conn):
                persist_parse_result(conn, self.store, s.project, lease, record)
                raise RuntimeError("crash between persist and the status change")

            with self.assertRaises(RuntimeError):
                jobs.finish(c, s.project, lease, "succeeded", "parsed", hook=hook)
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (s.job,)).fetchone()[0], "running")
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (s.job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (s.job,)).fetchone()[0], 0)

    def test_persist_is_idempotent_and_refuses_a_different_result(self):
        s = self.seed()
        same = record_from_batch(make_batch(s.doc, s.op))
        other = record_from_batch(make_batch(s.doc, s.op, texts=("Different", "Gamma")))
        with self.connect() as c:
            lease = jobs.acquire(c, s.project, s.job, "w1")
            persist_parse_result(c, self.store, s.project, lease, same)
            persist_parse_result(c, self.store, s.project, lease, same)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (s.job,)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (s.job,)).fetchone()[0], PAGES)
            with self.assertRaises(ParseResultConflict):
                persist_parse_result(c, self.store, s.project, lease, other)

    def test_a_stale_lease_cannot_persist(self):
        s = self.seed()
        record = record_from_batch(make_batch(s.doc, s.op))
        with self.connect() as c:
            lease = jobs.acquire(c, s.project, s.job, "w1")
            with self.assertRaises(jobs.LeaseLost):
                persist_parse_result(c, self.store, s.project, jobs.Lease(lease.job_id, lease.token + 1), record)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (s.job,)).fetchone()[0], 0)

    def test_tables_are_append_only_and_pages_stay_inside_the_document(self):
        s = self.seed()
        self.finish_with_result(s)
        with self.connect() as c:
            for stmt in ("UPDATE parse_results SET manifest = '{}'", "DELETE FROM parse_results",
                         "UPDATE parse_page_results SET status = 'ok'", "DELETE FROM parse_page_results"):
                with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute(stmt)
            with self.assertRaises(errors.CheckViolation):
                with c.transaction():
                    c.execute("INSERT INTO parse_page_results VALUES (%s, %s, %s, %s, 'ok', '[]', '[]', '[]')",
                              (s.job, s.project, s.doc, PAGES))  # one past the last page

    def test_get_summary_and_page_return_the_stored_evidence(self):
        s = self.seed()
        self.finish_with_result(s)
        base = f"/api/v1/documents/{s.doc}/parse-results/{s.job}"
        res = self.client.get(base, headers=self.headers(s.owner))
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["parser"], body["parser_version"], body["effective_profile"]),
                         ("unstructured", "0.27.36", "unstructured-oss-fast"))
        self.assertEqual([p["page_index"] for p in body["pages"]], [0, 1, 2])
        first = self.client.get(f"{base}/pages/0", headers=self.headers(s.owner)).json()
        self.assertEqual(first["page_index"], 0)
        self.assertEqual([e["raw_content"] for e in first["evidence"]], ["Alpha"])
        self.assertEqual([o["raw_content"] for o in first["observations"]], ["Alpha"])
        self.assertEqual(first["evidence"][0]["regions"][0]["bbox"], [0.1, 0.1, 0.5, 0.2])
        empty = self.client.get(f"{base}/pages/1", headers=self.headers(s.owner)).json()
        self.assertEqual((empty["evidence"], empty["observations"]), ([], []))
        third = self.client.get(f"{base}/pages/2", headers=self.headers(s.owner)).json()
        self.assertEqual([e["raw_content"] for e in third["evidence"]], ["Gamma"])
        self.assertTrue(self.client.get(f"{base}/pages/0", headers=self.headers(s.reviewer)).status_code == 200)

    def test_access_and_missing_results(self):
        s = self.seed()
        self.finish_with_result(s)
        base = f"/api/v1/documents/{s.doc}/parse-results/{s.job}"
        get = lambda url, actor=None, **h: self.client.get(url, headers=self.headers(actor, **h) if actor else {})
        self.assertEqual(get(base).status_code, 401)
        self.assertEqual(get(base, s.outsider).status_code, 404)           # non-member
        self.assertEqual(get(base, s.observer).status_code, 403)           # observer needs an explicit grant
        self.assertEqual(get(f"/api/v1/documents/{s.doc}/parse-results/{uuid.uuid4()}", s.owner).status_code, 404)
        self.assertEqual(get(f"/api/v1/documents/{uuid.uuid4()}/parse-results/{s.job}", s.owner).status_code, 404)
        self.assertEqual(get(f"{base}/pages/{PAGES}", s.owner).status_code, 404)
        self.assertEqual(get(f"{base}/pages/-1", s.owner).status_code, 400)
        self.assertEqual(get(f"{base}/pages/0", s.outsider).status_code, 404)

    def test_a_queued_job_has_no_result_to_read(self):
        s = self.seed()
        res = self.client.get(f"/api/v1/documents/{s.doc}/parse-results/{s.job}", headers=self.headers(s.owner))
        self.assertEqual(res.status_code, 404)

    def test_a_quarantined_or_inconsistent_batch_is_never_converted(self):
        s = self.seed()
        batch = make_batch(s.doc, s.op)
        bad = UnstructuredParser().normalize(
            b"not json", _make_request([0, 1, 2]), _make_unstructured_manifest([0, 1, 2]),
            UnstructuredProfile(strategy="fast"), document_id=str(s.doc), document_sha256="a" * 64,
            frames={}, document_page_count=PAGES)
        with self.assertRaises(ValueError):
            record_from_batch(bad)
        import dataclasses
        stripped = dataclasses.replace(batch, artifacts=())
        with self.assertRaises(ValueError):
            record_from_batch(stripped)


class SimpleSeed:
    def __init__(self, project, owner, observer, reviewer, outsider, doc, job, op):
        self.project, self.owner, self.observer, self.reviewer, self.outsider = project, owner, observer, reviewer, outsider
        self.doc, self.job, self.op = doc, job, op


if __name__ == "__main__":
    unittest.main()
