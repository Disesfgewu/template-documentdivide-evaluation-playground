"""promote_parse_result on real PostgreSQL (P-A, Refs #7, Refs #10).

The parse results are persisted through the real ``persist_parse_result`` (the same path the worker uses); the parser
output is the real normalizer's, built for the uploaded document's real sha256. Pages are then marked the way the
runner marks them (hidden text, dropped regions) so the G8 hard rule is tested at the database."""
from __future__ import annotations

from dataclasses import replace
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg
from psycopg import errors

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_results import record_from_batch
from lab_contracts.parse import PageStatus
from lab_db import jobs
from lab_db.parse_results import persist_parse_result
from lab_db.promote import (PROMOTION_RULE, RESIDUAL_LIMIT, UNSAFE_DIAGNOSTICS, PromoteConflict, PromoteError,
                            promote_parse_result)
from lab_db.repo import Change, record_change
from lab_db.storage import LocalArtifactStore
from lab_parser.normalize import DisplayFrame, UnstructuredParser, UnstructuredProfile
from fixtures.generators import synthetic_pdf
from tests.parser.test_normalize import _make_request, _make_unstructured_manifest

try:
    from tests.db.base import FreshDatabase, ADMIN_URL, h
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

PAGES = 3


def element(page, text, eid):
    return {"type": "NarrativeText", "element_id": eid, "text": text, "metadata": {
        "page_number": page, "coordinates": {"system": "PixelSpace", "layout_width": 612, "layout_height": 792,
                                             "points": [[61.2, 79.2], [61.2, 158.4], [306, 158.4], [306, 79.2]]}}}


def make_batch(document_id, sha, operation_id, pages_with_text=(1, 3)):
    """Evidence on the given 1-based pages; the rest have no element (the normalizer marks them needs_review)."""
    elements = [element(p, f"Text on page {p}", f"e{p}") for p in pages_with_text]
    request = _make_request([0, 1, 2]).model_copy(update={"operation_id": str(operation_id)})
    frames = {p: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0,
                              transform_ref=f"tr-{p}") for p in range(PAGES)}
    return UnstructuredParser().normalize(
        json.dumps(elements).encode(), request, _make_unstructured_manifest([0, 1, 2]), UnstructuredProfile(strategy="fast"),
        document_id=str(document_id), document_sha256=sha, frames=frames, document_page_count=PAGES)


def marked(batch, page_index, status, diagnostics):
    pages = [PageStatus(page_index=p.page_index, status=status, diagnostics=diagnostics) if p.page_index == page_index else p
             for p in batch.result.pages]
    return replace(batch, result=batch.result.model_copy(update={"pages": pages}))


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class PromoteRealDbTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url, "LAB_ARTIFACT_ROOT": str(self.root),
               "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)

    def headers(self, actor, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}", **extra}

    def seed(self):
        """project + owner + uploaded document + a queued parse job; returns a namespace of ids and the document sha."""
        with self.connect() as c:
            project, owner = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=p{uuid.uuid4().hex[:6]}",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}), content=synthetic_pdf.build(PAGES))
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        return project, owner, doc, res.json()["sha256"]

    def queue_job(self, project, owner, doc):
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json={})
        self.assertEqual(res.status_code, 202, res.text)
        job = uuid.UUID(res.json()["job_id"])
        with self.connect() as c:
            op = c.execute("SELECT operation_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()[0]
        return job, op

    def finish(self, project, job, batch):
        record = record_from_batch(batch)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            jobs.finish(c, project, lease, "succeeded", "parsed",
                        hook=lambda conn: persist_parse_result(conn, self.store, project, lease, record))

    def parsed(self, project, owner, doc, sha, mutate=None, pages_with_text=(1, 3)):
        job, op = self.queue_job(project, owner, doc)
        batch = make_batch(doc, sha, op, pages_with_text)
        batch = mutate(batch) if mutate else batch
        self.finish(project, job, batch)
        return job

    def benchmark(self, project, owner, doc):
        with self.connect() as c:
            rv = record_change(c, Change(project, owner, "benchmark.create", "benchmark_revision", f"b{uuid.uuid4().hex[:6]}", 1,
                                         h(uuid.uuid4().hex), "lab-c14n-1", "benchmark.created"))
            bench = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) "
                          "VALUES (%s, %s, %s, %s, 20, 2)", (bench, project, rv, doc))
        return bench

    def promote(self, project, bench, job, owner):
        with self.connect() as c:
            return promote_parse_result(c, project, bench, job, owner)

    def counts(self, doc):
        with self.connect() as c:
            return (c.execute("SELECT count(*) FROM evidence_units WHERE document_version_id = %s", (doc,)).fetchone()[0],
                    c.execute("SELECT count(*) FROM evidence_regions WHERE document_version_id = %s", (doc,)).fetchone()[0],
                    c.execute("SELECT count(*) FROM evidence_unit_sources s JOIN evidence_units u ON u.id = s.evidence_unit_id "
                              "WHERE u.document_version_id = %s", (doc,)).fetchone()[0])

    # ------------------------------------------------------------------------------------------------------------

    def test_only_clean_pages_are_promoted_and_the_pin_records_what_was_skipped_and_the_residual_limit(self):
        project, owner, doc, sha = self.seed()
        # page 1 clean, page 2 no element (needs_review), page 3 has evidence but carries hidden text
        job = self.parsed(project, owner, doc, sha, mutate=lambda b: marked(b, 2, "needs_review", ["text_not_visible"]))
        bench = self.benchmark(project, owner, doc)
        report = self.promote(project, bench, job, owner)
        self.assertEqual((report.promoted_pages, report.evidence_units, report.already_promoted), ([0], 1, False))
        self.assertEqual(report.skipped_pages, {1: "status_needs_review", 2: "unsafe_text:text_not_visible"})
        self.assertEqual(self.counts(doc), (1, 1, 1))                       # nothing from the quarantined or empty pages
        with self.connect() as c:
            text, page = c.execute("SELECT eu.raw_content, er.page_index FROM evidence_units eu JOIN evidence_regions er "
                                   "ON er.evidence_unit_id = eu.id WHERE eu.document_version_id = %s", (doc,)).fetchone()
            rule, prov = c.execute("SELECT rule, provenance FROM benchmark_parse_sources WHERE benchmark_id = %s", (bench,)).fetchone()
        self.assertEqual((text, page), ("Text on page 1", 0))
        self.assertEqual(rule, PROMOTION_RULE)
        self.assertEqual(prov["residual_limit"], RESIDUAL_LIMIT)
        self.assertEqual((prov["pages_total"], prov["promoted_pages"], prov["evidence_units"]), (3, [0], 1))
        self.assertEqual(prov["skipped_pages"], {"1": "status_needs_review", "2": "unsafe_text:text_not_visible"})
        self.assertEqual(sorted(prov["unsafe_diagnostics"]), sorted(UNSAFE_DIAGNOSTICS))

    def test_each_unsafe_diagnostic_blocks_a_page_even_when_its_status_says_ok(self):
        for diagnostic in sorted(UNSAFE_DIAGNOSTICS):
            with self.subTest(diagnostic):
                project, owner, doc, sha = self.seed()
                job = self.parsed(project, owner, doc, sha, mutate=lambda b, d=diagnostic: marked(b, 2, "ok", [d]))
                bench = self.benchmark(project, owner, doc)
                report = self.promote(project, bench, job, owner)
                self.assertNotIn(2, report.promoted_pages)
                self.assertEqual(report.skipped_pages[2], f"unsafe_text:{diagnostic}")
                self.assertEqual(self.counts(doc)[0], 1)                    # only page 1's unit

    def test_pages_that_are_not_ok_are_never_promoted_whatever_evidence_they_carry(self):
        for status in ("needs_review", "partial", "failed"):
            with self.subTest(status):
                project, owner, doc, sha = self.seed()
                job = self.parsed(project, owner, doc, sha, mutate=lambda b, s=status: marked(b, 0, s, ["something"]))
                bench = self.benchmark(project, owner, doc)
                report = self.promote(project, bench, job, owner)
                self.assertEqual(report.skipped_pages[0], f"status_{status}")
                self.assertNotIn(0, report.promoted_pages)

    def test_promotion_is_idempotent_for_the_same_job_and_refuses_another_job(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.benchmark(project, owner, doc)
        first = self.promote(project, bench, job, owner)
        before = self.counts(doc)
        again = self.promote(project, bench, job, owner)
        self.assertTrue(again.already_promoted)
        self.assertEqual((again.promoted_pages, again.skipped_pages, again.evidence_units),
                         (first.promoted_pages, first.skipped_pages, first.evidence_units))
        self.assertEqual(self.counts(doc), before)                          # nothing written twice
        job2 = self.parsed(project, owner, doc, sha)                        # a re-parse is another job
        with self.assertRaises(PromoteConflict):
            self.promote(project, bench, job2, owner)                       # the pin never moves (D1)
        self.assertEqual(self.counts(doc), before)

    def test_two_benchmarks_over_the_same_parse_job_share_the_evidence_units(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        b1, b2 = self.benchmark(project, owner, doc), self.benchmark(project, owner, doc)
        self.promote(project, b1, job, owner)
        r2 = self.promote(project, b2, job, owner)
        self.assertEqual(r2.promoted_pages, [0, 2])
        self.assertEqual(self.counts(doc), (2, 2, 2))                       # not duplicated

    def test_refusals_are_fixed_and_write_nothing(self):
        project, owner, doc, sha = self.seed()
        bench = self.benchmark(project, owner, doc)
        with self.assertRaises(PromoteError) as ctx:
            self.promote(project, bench, uuid.uuid4(), owner)               # unknown job
        self.assertEqual(str(ctx.exception), "parse_result_not_found")
        with self.assertRaises(PromoteError) as ctx:
            self.promote(project, uuid.uuid4(), uuid.uuid4(), owner)        # unknown benchmark
        self.assertEqual(str(ctx.exception), "benchmark_not_found")
        job, _ = self.queue_job(project, owner, doc)                        # queued, not parsed
        with self.assertRaises(PromoteError):
            self.promote(project, bench, job, owner)
        other_project, other_owner, other_doc, other_sha = self.seed()      # another project's parse job
        other_job = self.parsed(other_project, other_owner, other_doc, other_sha)
        with self.assertRaises(PromoteError) as ctx:
            self.promote(project, bench, other_job, owner)
        self.assertEqual(str(ctx.exception), "parse_result_not_found")
        self.assertEqual(self.counts(doc), (0, 0, 0))
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM benchmark_parse_sources WHERE project_id = %s", (project,)).fetchone()[0], 0)

    def test_a_cancelled_benchmark_takes_no_pin(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.benchmark(project, owner, doc)
        with self.connect() as c, c.transaction():
            c.execute("SELECT set_config('lab.actor_id', %s, true)", (str(owner),))
            c.execute("UPDATE benchmark_revisions SET status = 'cancelled', row_version = row_version + 1 WHERE id = %s", (bench,))
        with self.assertRaises(PromoteError) as ctx:
            self.promote(project, bench, job, owner)
        self.assertEqual(str(ctx.exception), "benchmark_state_takes_no_pin")
        self.assertEqual(self.counts(doc), (0, 0, 0))

    def test_the_pin_and_the_sources_are_append_only_and_the_database_checks_the_document(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.benchmark(project, owner, doc)
        self.promote(project, bench, job, owner)
        with self.connect() as c:
            for sql in ("UPDATE benchmark_parse_sources SET rule = 'x'", "DELETE FROM benchmark_parse_sources",
                        "UPDATE evidence_unit_sources SET page_index = 9", "DELETE FROM evidence_unit_sources"):
                with self.subTest(sql), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute(sql)
        # a pin for a job of ANOTHER document version is refused by the trigger even if the function is bypassed
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=second{uuid.uuid4().hex[:5]}",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}),
                               content=synthetic_pdf.build(PAGES + 1))
        self.assertEqual(res.status_code, 201, res.text)
        doc2 = uuid.UUID(res.json()["id"])
        bench2 = self.benchmark(project, owner, doc2)
        with self.connect() as c, self.assertRaises(errors.CheckViolation):
            with c.transaction():
                c.execute("INSERT INTO benchmark_parse_sources (benchmark_id, project_id, parse_job_id, document_version_id, rule, "
                          "provenance, promoted_by) VALUES (%s, %s, %s, %s, 'x', '{}', %s)", (bench2, project, job, doc2, owner))

if __name__ == "__main__":
    unittest.main()
