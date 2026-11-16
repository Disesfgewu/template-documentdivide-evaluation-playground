"""Real PostgreSQL integration tests for authenticated PDF upload and download (SPEC R01, 8, 10.1-10.2, 16.4, T01-T04, #67)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import types
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

# Ensure db, services/api, services/parser, contracts are available
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import lab_db.ingest
from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.documents import get_db
from lab_parser.isolation import InspectionError
from lab_api.main import create_app
from lab_api.readiness import connect
from lab_db.lineage import walk
from lab_db.repo import grant_app_privileges
from lab_db.storage import LocalArtifactStore
from fixtures.generators import bad_pdfs, synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL, with_db
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RealPdfUploadDownloadTests(FreshDatabase):
    """End-to-end integration tests using a fresh real PostgreSQL database per test class."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.f01_21 = synthetic_pdf.build(21)
        cls.f01_sha256 = hashlib.sha256(cls.f01_21).hexdigest()
        cls.f01_size = len(cls.f01_21)

        cls.f01_1 = synthetic_pdf.build(1)
        cls.f01_1_sha256 = hashlib.sha256(cls.f01_1).hexdigest()

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifact_root = Path(self.tmp.name).resolve()
        self.store = LocalArtifactStore(self.artifact_root)

        self.env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": self.url,
            "LAB_ARTIFACT_ROOT": str(self.artifact_root),
            "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
            "LAB_DB_CONNECT_TIMEOUT_S": "5",
        }
        with patch.dict(os.environ, self.env, clear=True):
            self.settings = load_settings()
        self.app = create_app(settings=self.settings, checks={})
        self.client = TestClient(self.app)
        self.secret = get_signing_secret(self.settings)

    @staticmethod
    def _decode_jsonb(val: Any) -> Any:
        if isinstance(val, str):
            return json.loads(val)
        return val

    def test_t01_f01_upload_and_byte_identical_download_with_real_postgres(self) -> None:
        """T01: Upload synthetic F01 (21 pages), verify DB audit/lineage/versions, and download byte-identically."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)

        res = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=f01-pages-21",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        doc_id = uuid.UUID(body["id"])
        self.assertEqual(body["sha256"], self.f01_sha256)
        self.assertEqual(body["size_bytes"], self.f01_size)
        self.assertEqual(body["page_count"], 21)
        self.assertEqual(body["revision"], 1)
        self.assertFalse(body["flagged"])
        self.assertEqual(body["warnings"], [])

        # Verify PostgreSQL database state directly
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # 1. document_versions
                cur.execute("SELECT * FROM document_versions WHERE id = %s", (doc_id,))
                doc_row = cur.fetchone()
                self.assertIsNotNone(doc_row)
                self.assertEqual(doc_row["project_id"], project_id)
                self.assertEqual(doc_row["sha256"], self.f01_sha256)
                self.assertEqual(doc_row["page_count"], 21)

                # 2. pages
                cur.execute(
                    "SELECT * FROM pages WHERE document_version_id = %s ORDER BY page_index ASC",
                    (doc_id,),
                )
                pages = cur.fetchall()
                self.assertEqual(len(pages), 21)
                for expected_idx, p in enumerate(pages):
                    self.assertEqual(p["page_index"], expected_idx)
                    self.assertGreater(p["width_pt"], 0)
                    self.assertGreater(p["height_pt"], 0)

                # 3. resource_versions
                cur.execute("SELECT * FROM resource_versions WHERE id = %s", (doc_row["resource_id"],))
                rv_row = cur.fetchone()
                self.assertIsNotNone(rv_row)
                self.assertEqual(rv_row["logical_id"], "f01-pages-21")
                self.assertEqual(rv_row["revision"], 1)
                self.assertEqual(rv_row["content_hash"], self.f01_sha256)

                # 4. artifacts
                cur.execute("SELECT * FROM artifacts WHERE id = %s", (doc_row["artifact_id"],))
                art_row = cur.fetchone()
                self.assertIsNotNone(art_row)
                self.assertEqual(art_row["sha256"], self.f01_sha256)
                self.assertEqual(art_row["state"], "committed")

                # 5. audit_events
                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s AND resource_id = %s",
                    (project_id, doc_row["resource_id"]),
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row["action"], "document.imported")
                self.assertEqual(audit_row["outcome"], "succeeded")

                # 6. outbox_events
                cur.execute("SELECT * FROM outbox_events WHERE project_id = %s", (project_id,))
                outbox_row = cur.fetchone()
                self.assertIsNotNone(outbox_row)
                self.assertEqual(outbox_row["topic"], "document.imported")
                payload = self._decode_jsonb(outbox_row["payload"])
                self.assertEqual(payload["page_count"], 21)
                self.assertFalse(payload["flagged"])

        # Download original PDF via API and verify exact bytes
        dl_res = self.client.get(
            f"/api/v1/documents/{doc_id}/original",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.headers["Content-Type"], "application/pdf")
        self.assertEqual(dl_res.content, self.f01_21)
        self.assertEqual(hashlib.sha256(dl_res.content).hexdigest(), self.f01_sha256)

    def test_multipart_upload_and_bounded_size_checks(self) -> None:
        """Multipart upload works within bounds; oversized multipart stream fails closed with 413."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)

        multipart_body = (
            b"--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="logical_id"\r\n\r\n'
            b"mp-doc-1\r\n"
            b"--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="file"; filename="upload.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
            + self.f01_1
            + b"\r\n--BOUNDARY--\r\n"
        )

        res = self.client.post(
            f"/api/v1/documents?project_id={project_id}",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "multipart/form-data; boundary=BOUNDARY",
            },
            content=multipart_body,
        )
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertEqual(body["logical_id"], "mp-doc-1")
        self.assertEqual(body["sha256"], self.f01_1_sha256)

        # Bounded streaming check: client with declared or chunked body exceeding server limit is rejected with 413
        oversized_headers = {
            "Authorization": f"Bearer {owner_token}",
            "Content-Type": "multipart/form-data; boundary=BOUNDARY",
            "Content-Length": str(600 * 1024 * 1024),  # Exceeds DEFAULT_MAX_PDF_BYTES + 65536
        }
        res_oversized = self.client.post(
            f"/api/v1/documents?project_id={project_id}",
            headers=oversized_headers,
            content=b"--BOUNDARY\r\n",
        )
        self.assertEqual(res_oversized.status_code, 413)
        self.assertEqual(res_oversized.json()["error"]["code"], "payload_too_large")

    def test_t04_idempotency_lineage_and_artifact_deduplication(self) -> None:
        """T04: Idempotent replay, artifact reuse, revision progression, and database lineage proof."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)

        # 1. First upload with Idempotency-Key
        res1 = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-idemp",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "idemp-key-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res1.status_code, 201, res1.text)
        doc1_id = res1.json()["id"]

        # 2. Replay with same Idempotency-Key and same bytes -> 200 OK replayed
        res2 = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-idemp",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "idemp-key-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["id"], doc1_id)
        self.assertTrue(res2.json()["replayed"])

        # 3. Same Idempotency-Key with different payload -> 409 Conflict
        res_conflict = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-idemp",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "idemp-key-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res_conflict.status_code, 409)
        self.assertEqual(res_conflict.json()["error"]["code"], "conflict")

        # 4. Upload same bytes under new logical_id -> reuses the existing committed artifact
        res3 = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-lineage-test",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res3.status_code, 201)
        doc3_id = uuid.UUID(res3.json()["id"])
        res3_rv_id = uuid.UUID(res3.json()["resource_id"])

        with self.connect() as conn:
            arts = conn.execute(
                "SELECT artifact_id FROM document_versions WHERE id = ANY(%s)",
                ([uuid.UUID(doc1_id), doc3_id],),
            ).fetchall()
            self.assertEqual(len({a[0] for a in arts}), 1, "same bytes in project must reuse single artifact row")

        # 5. Upload different bytes under same logical_id -> creates revision 2 with supersedes lineage edge
        res4 = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-lineage-test",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_1,
        )
        self.assertEqual(res4.status_code, 201)
        self.assertEqual(res4.json()["revision"], 2)
        self.assertEqual(res4.json()["sha256"], self.f01_1_sha256)
        res4_rv_id = uuid.UUID(res4.json()["resource_id"])

        # 6. Lineage proof in PostgreSQL: verify lineage edge and walk
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT upstream_id, downstream_id, relation FROM lineage_edges WHERE downstream_id = %s",
                    (res4_rv_id,),
                )
                edge_row = cur.fetchone()
                self.assertIsNotNone(edge_row, "lineage edge must connect revision 2 to revision 1")
                self.assertEqual(edge_row["upstream_id"], res3_rv_id)
                self.assertEqual(edge_row["downstream_id"], res4_rv_id)
                self.assertEqual(edge_row["relation"], "supersedes")

            # Bounded lineage traversal from revision 2 upstream traverses to revision 1
            walk_res = walk(conn, project_id, res4_rv_id, direction="upstream", max_depth=3)
            self.assertIn(res3_rv_id, walk_res.nodes)
            self.assertIn(res4_rv_id, walk_res.nodes)
            self.assertEqual(len(walk_res.edges), 1)
            self.assertEqual(walk_res.edges[0].upstream_id, res3_rv_id)
            self.assertEqual(walk_res.edges[0].downstream_id, res4_rv_id)
            self.assertEqual(walk_res.edges[0].relation, "supersedes")
            self.assertFalse(walk_res.truncated)

    def test_concurrent_idempotency_requests_safely_serialized(self) -> None:
        """Concurrency test: 8 simultaneous uploads with the same Idempotency-Key create exactly 1 document version.

        This proves database serialization, so the admission budget is raised
        to admit all 8; refusal under the default budget is the next test."""
        self.app.state.upload_concurrency = 8
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        barrier = threading.Barrier(8)
        statuses: list[int] = []
        doc_ids: list[str] = []
        errors: list[str] = []

        def worker():
            client = TestClient(self.app)
            try:
                barrier.wait()
                r = client.post(
                    f"/api/v1/documents?project_id={project_id}&logical_id=doc-concurrent",
                    headers={
                        "Authorization": f"Bearer {owner_token}",
                        "Content-Type": "application/pdf",
                        "Idempotency-Key": "concurrent-key-1",
                    },
                    content=self.f01_21,
                )
                statuses.append(r.status_code)
                if r.status_code in (200, 201):
                    doc_ids.append(r.json()["id"])
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(errors, [])
        self.assertEqual(len(statuses), 8)
        self.assertTrue(all(s in (200, 201) for s in statuses))
        self.assertEqual(statuses.count(201), 1, "exactly one concurrent request must create the document version")
        self.assertEqual(statuses.count(200), 7, "all other concurrent requests must replay the created document")
        self.assertEqual(len(set(doc_ids)), 1, "all responses must identify the exact same document version")

        # In PostgreSQL: exactly 1 document version and 1 operation row (status succeeded, none stranded as intent)
        with self.connect() as conn:
            doc_cnt = conn.execute(
                "SELECT count(*) FROM document_versions WHERE project_id = %s",
                (project_id,),
            ).fetchone()[0]
            self.assertEqual(doc_cnt, 1)

            intent_cnt = conn.execute(
                "SELECT count(*) FROM operations WHERE project_id = %s AND status = 'intent'",
                (project_id,),
            ).fetchone()[0]
            self.assertEqual(intent_cnt, 0, "no operations may be left stranded in intent status")

    def test_excess_concurrent_uploads_are_refused_not_queued_or_duplicated(self) -> None:
        """With the default admission budget, uploads beyond it get a retryable
        503 before their body is read. Whatever the timing, at most one
        document version exists and nothing is stranded; a retry then succeeds."""
        self.assertNotIn("upload_concurrency", vars(self.app.state))
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        token = issue_actor_token(owner_id, self.secret)
        barrier = threading.Barrier(8)
        results: list[tuple[int, dict]] = []
        errors: list[str] = []

        def post():
            try:
                barrier.wait()
                r = TestClient(self.app).post(
                    f"/api/v1/documents?project_id={project_id}&logical_id=doc-busy",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf",
                             "Idempotency-Key": "busy-key-1"},
                    content=self.f01_21,
                )
                results.append((r.status_code, r.json()))
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=post) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code in (200, 201, 503) for code, _ in results), [c for c, _ in results])
        self.assertLessEqual([c for c, _ in results].count(201), 1)
        for code, body in results:
            if code == 503:
                self.assertEqual((body["error"]["code"], body["error"]["retryable"]), ("server_busy", True))
        with self.connect() as conn:
            self.assertLessEqual(
                conn.execute("SELECT count(*) FROM document_versions WHERE project_id = %s", (project_id,)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM operations WHERE project_id = %s AND status = 'intent'",
                             (project_id,)).fetchone()[0], 0)

        # The slots came back: the same request is now admitted and ends with exactly one version.
        retry = TestClient(self.app).post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-busy",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf",
                     "Idempotency-Key": "busy-key-1"},
            content=self.f01_21,
        )
        self.assertIn(retry.status_code, (200, 201))
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM document_versions WHERE project_id = %s", (project_id,)).fetchone()[0], 1)

    def test_caller_asserted_actor_id_rejected_with_401(self) -> None:
        """Caller-asserted actor UUID or X-Actor-Id spoofing is rejected with 401."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        # Attempt raw actor UUID as bearer token
        res_raw_uuid = self.client.post(
            f"/api/v1/documents?project_id={project_id}",
            headers={
                "Authorization": f"Bearer {owner_id}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_raw_uuid.status_code, 401)
        self.assertEqual(res_raw_uuid.json()["error"]["code"], "unauthorized")

        # Attempt X-Actor-Id header spoofing
        res_x_actor = self.client.post(
            f"/api/v1/documents?project_id={project_id}",
            headers={
                "X-Actor-Id": str(owner_id),
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_x_actor.status_code, 401)
        self.assertEqual(res_x_actor.json()["error"]["code"], "unauthorized")

    def test_cross_project_and_role_authorization_gates(self) -> None:
        """Cross-project download is rejected with 404; unauthorized reviewer upload is 403."""
        with self.connect() as conn:
            proj_a, owner_a = self.make_project(conn, role="owner")
            proj_b, owner_b = self.make_project(conn, role="owner")
            reviewer_a = self.add_member(conn, proj_a, role="reviewer")

        token_a = issue_actor_token(owner_a, self.secret)
        token_b = issue_actor_token(owner_b, self.secret)
        token_rev = issue_actor_token(reviewer_a, self.secret)

        # Upload document to Project A by owner A
        res_up = self.client.post(
            f"/api/v1/documents?project_id={proj_a}",
            headers={
                "Authorization": f"Bearer {token_a}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_up.status_code, 201)
        doc_a_id = res_up.json()["id"]

        # 1. Cross-project download: Owner B attempts to download Project A document -> 404
        res_cross = self.client.get(
            f"/api/v1/documents/{doc_a_id}/original",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        self.assertEqual(res_cross.status_code, 404)
        self.assertEqual(res_cross.json()["error"]["code"], "not_found")

        # 2. Reviewer role attempts to upload document -> 403
        res_rev = self.client.post(
            f"/api/v1/documents?project_id={proj_a}",
            headers={
                "Authorization": f"Bearer {token_rev}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_rev.status_code, 403)
        self.assertEqual(res_rev.json()["error"]["code"], "permission_denied")

    def test_f05_rejections_and_audit_events_in_real_db(self) -> None:
        """F05 failures leave 0 visible document versions, record failed operation, and replay original rejection without outbox."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            reviewer_id = self.add_member(conn, project_id, role="reviewer")
            _, other_owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        rev_token = issue_actor_token(reviewer_id, self.secret)
        other_token = issue_actor_token(other_owner_id, self.secret)

        # Encrypted PDF with Idempotency-Key
        enc_bytes = bad_pdfs.build("encrypted")
        res_enc = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-enc",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "f05-idemp-1",
            },
            content=enc_bytes,
        )
        self.assertEqual(res_enc.status_code, 422)
        self.assertEqual(res_enc.json()["error"]["code"], "unsupported_encryption")

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # 0 document versions
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

                # Rejection must NOT create outbox records
                cur.execute("SELECT count(*) AS cnt FROM outbox_events WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

                # Rejection audit event recorded
                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s AND action = 'document.rejected'",
                    (project_id,),
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row["outcome"], "failed")
                self.assertEqual(audit_row["reason"], "encrypted_pdf")

                # Failed operation recorded (terminal 'failed', NOT stranded as 'intent')
                cur.execute(
                    "SELECT * FROM operations WHERE project_id = %s AND kind = 'document.import'",
                    (project_id,),
                )
                op_row = cur.fetchone()
                self.assertIsNotNone(op_row)
                self.assertEqual(op_row["status"], "failed")

                intent_cnt = conn.execute(
                    "SELECT count(*) FROM operations WHERE project_id = %s AND status = 'intent'",
                    (project_id,),
                ).fetchone()[0]
                self.assertEqual(intent_cnt, 0)

        # 1. Replaying the identical rejected request returns original 422 status and response
        res_replay_failed = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-enc",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "f05-idemp-1",
            },
            content=enc_bytes,
        )
        self.assertEqual(res_replay_failed.status_code, 422)
        self.assertEqual(res_replay_failed.json()["error"]["code"], "unsupported_encryption")
        self.assertEqual(res_replay_failed.json()["error"]["message"], res_enc.json()["error"]["message"])
        self.assertEqual(res_replay_failed.json()["error"]["details"], res_enc.json()["error"]["details"])

        # 2. Replaying the same key with changed body returns 409 conflict
        res_conflict_body = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-enc",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "f05-idemp-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res_conflict_body.status_code, 409)
        self.assertEqual(res_conflict_body.json()["error"]["code"], "conflict")

        # 3. Authorization must precede disclosure:
        # A reviewer without import permissions gets 403; an actor outside the project gets 404
        res_rev = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-enc",
            headers={
                "Authorization": f"Bearer {rev_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "f05-idemp-1",
            },
            content=enc_bytes,
        )
        self.assertEqual(res_rev.status_code, 403)
        self.assertEqual(res_rev.json()["error"]["code"], "permission_denied")

        res_other = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-enc",
            headers={
                "Authorization": f"Bearer {other_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "f05-idemp-1",
            },
            content=enc_bytes,
        )
        self.assertEqual(res_other.status_code, 404)
        self.assertEqual(res_other.json()["error"]["code"], "not_found")

        # 4. Final verification: rejection and replays did not create outbox records or extra operations
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM outbox_events WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM operations WHERE project_id = %s AND kind = 'document.import'", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 1)
                cur.execute("SELECT count(*) AS cnt FROM audit_events WHERE project_id = %s AND action = 'document.rejected'", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 1)

    def test_rejected_replay_is_the_stored_original_and_never_recomputed(self) -> None:
        """PM-REVIEW on #73: replay of a rejected request re-ran the parser.
        The original response is saved with the operation (0009) and replayed
        verbatim, even if the parser, limits or code would answer differently."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        token = issue_actor_token(owner_id, self.secret)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/pdf",
            "Idempotency-Key": "stored-replay-1",
        }
        url = f"/api/v1/documents?project_id={project_id}&logical_id=doc-stored"
        enc = bad_pdfs.build("encrypted")

        first = self.client.post(url, headers={**headers, "X-Request-Id": "req-first"}, content=enc)
        self.assertEqual(first.status_code, 422)
        original = first.json()["error"]
        self.assertEqual(original["code"], "unsupported_encryption")
        self.assertEqual(original["request_id"], first.headers["x-request-id"])
        self.assertNotEqual(original["request_id"], "req-first")

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM operation_results WHERE project_id = %s", (project_id,))
                saved = cur.fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["http_status"], 422)
        stored = self._decode_jsonb(saved[0]["response_body"])
        self.assertEqual(stored["code"], "unsupported_encryption")
        self.assertNotIn("request_id", stored)  # per-request, supplied at replay time
        self.assertEqual({k: v for k, v in original.items() if k != "request_id"}, stored)

        def forbidden(*args, **kwargs):
            raise AssertionError("a replay must not run the parser again")

        # 1. Replay touches neither the inspector nor the geometry extractor.
        with patch("lab_api.documents.inspect_pdf_isolated", side_effect=forbidden):
            replay = self.client.post(url, headers={**headers, "X-Request-Id": "req-replay"}, content=enc)
        self.assertEqual(replay.status_code, 422)
        self.assertEqual(
            {k: v for k, v in replay.json()["error"].items() if k != "request_id"}, stored
        )
        self.assertEqual(replay.json()["error"]["request_id"], replay.headers["x-request-id"])
        self.assertNotEqual(replay.json()["error"]["request_id"], original["request_id"])
        self.assertNotEqual(replay.json()["error"]["request_id"], "req-replay")

        # 2. Even if today's parser would accept the file, the original stands.
        accepting = types.SimpleNamespace(
            outcome="accept", reason=None, sha256="0" * 64, size_bytes=1, page_count=1, warnings=()
        )
        with patch("lab_api.documents.inspect_pdf_isolated", return_value=(accepting, ())):
            changed = self.client.post(url, headers=headers, content=enc)
        self.assertEqual(changed.status_code, 422)
        self.assertEqual(changed.json()["error"]["code"], "unsupported_encryption")

        # 3. Replays created no new operation, result or audit event.
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                for table, extra in (("operations", " AND kind = 'document.import'"),
                                     ("operation_results", ""),
                                     ("audit_events", " AND action = 'document.rejected'")):
                    cur.execute(f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s{extra}", (project_id,))
                    self.assertEqual(cur.fetchone()["cnt"], 1, table)

    def test_success_replay_is_the_stored_original(self) -> None:
        """The replay of an accepted upload carries the stored original response, not a
        reconstruction from rows, and does not run the inspector again."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        headers = {"Authorization": f"Bearer {issue_actor_token(owner_id, self.secret)}",
                   "Content-Type": "application/pdf", "Idempotency-Key": "success-replay-1"}
        url = f"/api/v1/documents?project_id={project_id}&logical_id=doc-replay"

        first = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual(first.status_code, 201, first.text)
        original = first.json()
        self.assertIs(original["flagged"], False)
        self.assertEqual(original["warnings"], [])
        self.assertEqual(original["page_count"], 21)

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM operation_results WHERE project_id = %s", (project_id,))
                saved = cur.fetchall()
        self.assertEqual([r["http_status"] for r in saved], [201])
        self.assertEqual(self._decode_jsonb(saved[0]["response_body"]), original)

        with patch("lab_api.documents.inspect_pdf_isolated", side_effect=AssertionError("replay must not re-inspect")):
            replay = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual({k: v for k, v in replay.json().items() if k != "replayed"}, original)
        self.assertIs(replay.json()["replayed"], True)

    def test_a_cancelled_inspection_persists_nothing_and_a_retry_succeeds(self) -> None:
        """Client disconnect during inspection: 503 request_cancelled, rolled back, no cached failure."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        headers = {"Authorization": f"Bearer {issue_actor_token(owner_id, self.secret)}",
                   "Content-Type": "application/pdf", "Idempotency-Key": "cancel-1"}
        url = f"/api/v1/documents?project_id={project_id}&logical_id=doc-cancel"
        with patch("lab_parser.artifacts.inspect_geometry", side_effect=InspectionError("cancelled")):
            res = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual(res.status_code, 503, res.text)
        self.assertEqual(res.json()["error"]["code"], "request_cancelled")
        self.assertIs(res.json()["error"]["retryable"], True)
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                for table in ("operations", "idempotency_keys", "operation_results", "document_versions", "audit_events"):
                    cur.execute(f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s", (project_id,))
                    self.assertEqual(cur.fetchone()["cnt"], 0, table)
        retry = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual(retry.status_code, 201, retry.text)

    def test_a_disconnect_after_a_good_inspection_but_before_the_write_persists_nothing(self) -> None:
        from lab_api import documents
        real = documents.inspect_pdf_isolated

        def inspect_then_disconnect(data, limits, cancelled=None):
            result = real(data, limits, cancelled=cancelled)
            cancelled.__self__.set()  # the client goes away right after the child finished
            return result

        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        headers = {"Authorization": f"Bearer {issue_actor_token(owner_id, self.secret)}",
                   "Content-Type": "application/pdf", "Idempotency-Key": "cancel-late-1"}
        url = f"/api/v1/documents?project_id={project_id}&logical_id=doc-cancel-late"
        with patch("lab_api.documents.inspect_pdf_isolated", side_effect=inspect_then_disconnect):
            res = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual((res.status_code, res.json()["error"]["code"]), (503, "request_cancelled"))
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                for table in ("operations", "idempotency_keys", "document_versions", "artifacts", "audit_events"):
                    cur.execute(f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s", (project_id,))
                    self.assertEqual(cur.fetchone()["cnt"], 0, table)
        retry = self.client.post(url, headers=headers, content=self.f01_21)
        self.assertEqual(retry.status_code, 201, retry.text)

    def test_repaired_xref_is_rejected_as_corrupt_pdf_and_publishes_nothing(self) -> None:
        """Behaviour change with the isolation child: any pypdf warning is an invalid PDF,
        so the former 'flagged' 201 for a repaired xref is now a 422 corrupt_pdf with no
        document version and no artifact."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        headers = {"Authorization": f"Bearer {issue_actor_token(owner_id, self.secret)}",
                   "Content-Type": "application/pdf"}
        res = self.client.post(f"/api/v1/documents?project_id={project_id}&logical_id=doc-xref",
                               headers=headers, content=bad_pdfs.build("bad_xref"))
        self.assertEqual(res.status_code, 422, res.text)
        self.assertEqual(res.json()["error"]["code"], "corrupt_pdf")
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

    def test_lying_page_count_is_rejected_before_any_version_exists(self) -> None:
        """/Count says 1 while /Kids lists 3: pypdf and PDFium disagree, so the upload fails closed."""
        from tests.parser.test_isolation import crafted
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        headers = {"Authorization": f"Bearer {issue_actor_token(owner_id, self.secret)}",
                   "Content-Type": "application/pdf"}
        res = self.client.post(f"/api/v1/documents?project_id={project_id}&logical_id=doc-lie",
                               headers=headers, content=crafted(3, 1, True))
        self.assertEqual(res.status_code, 422, res.text)
        self.assertEqual(res.json()["error"]["code"], "corrupt_pdf")
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

    def test_failed_operation_without_a_saved_result_is_not_recomputed(self) -> None:
        """An operation that failed before 0009 has no saved response: the
        retry is a 409 explaining that, not a guess from re-parsing."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            op_id = uuid.uuid4()
            logical_id = "doc-legacy"
            request_hash = hashlib.sha256(f"{logical_id}:{hashlib.sha256(self.f01_1).hexdigest()}".encode()).hexdigest()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO operations (id, project_id, kind, actor_id, status) "
                    "VALUES (%s, %s, 'document.import', %s, 'failed')",
                    (op_id, project_id, owner_id),
                )
                conn.execute(
                    "INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id) "
                    "VALUES (%s, 'document.import', 'legacy-key', %s, %s)",
                    (project_id, request_hash, op_id),
                )
        token = issue_actor_token(owner_id, self.secret)
        with patch("lab_api.documents.inspect_pdf_isolated", side_effect=AssertionError("must not re-parse")):
            res = self.client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id={logical_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf",
                         "Idempotency-Key": "legacy-key"},
                content=self.f01_1,
            )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "conflict")
        self.assertIn("not retained", res.json()["error"]["message"])

    def test_isolation_child_rejection_is_persisted_and_replayed_atomically_in_real_db(self) -> None:
        """A PDF the isolation child rejects persists the failure without outbox; replay returns the original 422, changed body is 409."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            reviewer_id = self.add_member(conn, project_id, role="reviewer")
            _, other_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        rev_token = issue_actor_token(reviewer_id, self.secret)
        other_token = issue_actor_token(other_id, self.secret)

        def failing_child(*args, **kwargs):
            raise InspectionError("invalid_pdf")

        with patch("lab_parser.artifacts.inspect_geometry", side_effect=failing_child):
            res_bad_geom = self.client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-geom",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "geom-idemp-1",
                },
                content=self.f01_1,
            )
            self.assertEqual(res_bad_geom.status_code, 422)
            self.assertEqual(res_bad_geom.json()["error"]["code"], "corrupt_pdf")
            self.assertEqual(res_bad_geom.json()["error"]["message"], "document rejected: corrupt_pdf")
            self.assertEqual(res_bad_geom.json()["error"]["details"]["reason"], "corrupt_pdf")

        # Verify PostgreSQL audit and that rejection did NOT create outbox records
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s AND action = 'document.rejected'",
                    (project_id,),
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row["outcome"], "failed")
                self.assertEqual(audit_row["reason"], "corrupt_pdf")

                cur.execute(
                    "SELECT count(*) AS cnt FROM outbox_events WHERE project_id = %s",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 0)

        # 1. Identical request with same Idempotency-Key replays original 422 and exact response
        with patch("lab_parser.artifacts.inspect_geometry", side_effect=failing_child):
            res_replay = self.client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-geom",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "geom-idemp-1",
                },
                content=self.f01_1,
            )
            self.assertEqual(res_replay.status_code, 422)
            self.assertEqual(res_replay.json()["error"]["code"], "corrupt_pdf")
            self.assertEqual(
                res_replay.json()["error"]["message"],
                res_bad_geom.json()["error"]["message"],
            )
            self.assertEqual(
                res_replay.json()["error"]["details"],
                res_bad_geom.json()["error"]["details"],
            )

        # 2. Changed body with same Idempotency-Key returns 409 conflict
        res_conflict = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-geom",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "geom-idemp-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_conflict.status_code, 409)
        self.assertEqual(res_conflict.json()["error"]["code"], "conflict")

        # 3. Authorization must precede disclosure: reviewer gets 403, non-member gets 404
        res_unauth = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-geom",
            headers={
                "Authorization": f"Bearer {rev_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "geom-idemp-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res_unauth.status_code, 403)
        self.assertEqual(res_unauth.json()["error"]["code"], "permission_denied")

        res_non_member = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-geom",
            headers={
                "Authorization": f"Bearer {other_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "geom-idemp-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res_non_member.status_code, 404)
        self.assertEqual(res_non_member.json()["error"]["code"], "not_found")

        # 4. Final verification: rejection and replays did not create outbox records or extra operations
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT count(*) AS cnt FROM outbox_events WHERE project_id = %s",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute(
                    "SELECT count(*) AS cnt FROM operations WHERE project_id = %s AND kind = 'document.import'",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 1)
                cur.execute(
                    "SELECT count(*) AS cnt FROM audit_events WHERE project_id = %s AND action = 'document.rejected'",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 1)

    def test_rollback_on_ingest_failure_leaves_no_visible_version_or_stranded_intent(self) -> None:
        """Negative test: if database transaction rolls back, no document version, operation, or idempotency key is stranded."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)

        def failing_insert_pages(*args, **kwargs):
            raise psycopg.DatabaseError("simulated page insert failure")

        with patch("lab_db.ingest._insert_pages", side_effect=failing_insert_pages):
            res = self.client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-rollback",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "rollback-key-1",
                },
                content=self.f01_21,
            )
            self.assertIn(res.status_code, (500, 503))

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM resource_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                # Ensure no stranded intent operation or idempotency key was committed
                cur.execute("SELECT count(*) AS cnt FROM operations WHERE project_id = %s AND status = 'intent'", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM idempotency_keys WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

    def test_oversized_or_corrupt_artifact_on_disk_fails_closed_on_download(self) -> None:
        """Corrupt or oversized file on disk fails closed with 500 storage_corruption on both download paths; authorization check precedes storage access."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            _, other_owner_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        other_token = issue_actor_token(other_owner_id, self.secret)

        res = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-corrupt-test",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_1,
        )
        self.assertEqual(res.status_code, 201)
        doc_id = res.json()["id"]

        # Corrupt the file on disk by appending extra bytes (making it exceed size_bytes bound)
        disk_file = self.artifact_root / "sha256" / self.f01_1_sha256[:2] / self.f01_1_sha256
        with open(disk_file, "ab") as f:
            f.write(b"CORRUPT_EXTRA_TRAILING_BYTES")

        # 1. Dedicated download endpoint fails closed
        res_dl = self.client.get(
            f"/api/v1/documents/{doc_id}/original",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(res_dl.status_code, 500)
        self.assertEqual(res_dl.json()["error"]["code"], "storage_corruption")

        # 2. Accept: application/pdf endpoint also fails closed
        res_accept = self.client.get(
            f"/api/v1/documents/{doc_id}",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Accept": "application/pdf",
            },
        )
        self.assertEqual(res_accept.status_code, 500)
        self.assertEqual(res_accept.json()["error"]["code"], "storage_corruption")

        # 3. Authorization check precedes storage: an unauthorized actor gets 404 without storage error or disclosure
        res_unauth = self.client.get(
            f"/api/v1/documents/{doc_id}/original",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(res_unauth.status_code, 404)
        self.assertEqual(res_unauth.json()["error"]["code"], "not_found")

        # The committed size is immutable. Lower the server ceiling instead of
        # mutating artifact identity to exercise the same boundary with real PG.
        with patch.object(self.app.state, "max_pdf_bytes", len(self.f01_1) - 1, create=True):
            res_ceiling = self.client.get(
                f"/api/v1/documents/{doc_id}/original",
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        self.assertEqual(res_ceiling.status_code, 500)
        self.assertEqual(res_ceiling.json()["error"]["code"], "storage_corruption")

    def make_restricted_app_client(self) -> tuple[TestClient, str]:
        """Create a dedicated restricted application role with least-privilege DML permissions,
        and instantiate a TestClient whose database connections run under that role."""
        role = f"lab_app_{uuid.uuid4().hex[:10]}"
        password = f"pw_{uuid.uuid4().hex}"

        with self.connect(autocommit=True) as c:
            c.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                ),
            )
            c.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(self.dbname), sql.Identifier(role)
                )
            )
            c.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
            )

        def _cleanup() -> None:
            with self.connect(autocommit=True) as c:
                c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

        self.addCleanup(_cleanup)

        with self.connect() as c:
            grant_app_privileges(c, role)

        p = urlsplit(self.url)
        port_part = f":{p.port}" if p.port else ""
        netloc = f"{role}:{password}@{p.hostname}{port_part}"
        role_url = urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))

        try:
            with psycopg.connect(role_url, connect_timeout=3) as conn:
                with self.assertRaises(psycopg.Error):
                    with conn.transaction():
                        conn.execute("DROP TABLE audit_events")
            app_env = dict(self.env)
            app_env["LAB_DATABASE_URL"] = role_url
            with patch.dict(os.environ, app_env, clear=True):
                settings = load_settings()
            app = create_app(settings=settings, checks={})
            return TestClient(app, raise_server_exceptions=False), role
        except psycopg.OperationalError:
            app_env = dict(self.env)
            with patch.dict(os.environ, app_env, clear=True):
                settings = load_settings()
            app = create_app(settings=settings, checks={})

            def restricted_get_db():
                with connect(settings) as conn:
                    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                    yield conn

            app.dependency_overrides[get_db] = restricted_get_db
            return TestClient(app, raise_server_exceptions=False), role

    def test_restricted_role_upload_and_actor_context_real_db(self) -> None:
        """Restricted application-role real PostgreSQL test for upload and transaction-local actor context."""
        client, role = self.make_restricted_app_client()

        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        owner_token = issue_actor_token(owner_id, self.secret)

        observed_actor: list[str | None] = []
        original_insert_pages = lab_db.ingest._insert_pages

        def spy_insert_pages(conn, doc_version_id, pages):
            row = conn.execute("SELECT current_setting('lab.actor_id', true)").fetchone()
            observed_actor.append(row[0] if row else None)
            return original_insert_pages(conn, doc_version_id, pages)

        with patch("lab_db.ingest._insert_pages", side_effect=spy_insert_pages):
            res = client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-upload",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=self.f01_1,
            )

        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(observed_actor, [str(owner_id)])

        doc_id = uuid.UUID(res.json()["id"])
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM document_versions WHERE id = %s", (doc_id,))
                doc_row = cur.fetchone()
                self.assertIsNotNone(doc_row)
                self.assertEqual(doc_row["project_id"], project_id)

                cur.execute(
                    "SELECT * FROM operations WHERE project_id = %s AND kind = 'document.import'",
                    (project_id,),
                )
                op_row = cur.fetchone()
                self.assertIsNotNone(op_row)
                self.assertEqual(op_row["status"], "succeeded")
                self.assertEqual(op_row["actor_id"], owner_id)

                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s AND action = 'document.imported'",
                    (project_id,),
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row["actor_id"], owner_id)

    def test_restricted_role_replay_idempotency_succeeded_and_rejected(self) -> None:
        """Restricted application-role real PostgreSQL test for replay of succeeded and rejected imports."""
        client, role = self.make_restricted_app_client()

        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        owner_token = issue_actor_token(owner_id, self.secret)

        # 1. Succeeded upload replay under restricted role
        res1 = client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-replay",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "restricted-idem-key-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res1.status_code, 201, res1.text)
        doc_id = res1.json()["id"]

        # Exact replay
        res2 = client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-replay",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "restricted-idem-key-1",
            },
            content=self.f01_1,
        )
        self.assertEqual(res2.status_code, 200, res2.text)
        self.assertTrue(res2.json().get("replayed"))
        self.assertEqual(res2.json()["id"], doc_id)

        # Conflict on same key with different body
        res_conflict = client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-replay",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "restricted-idem-key-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res_conflict.status_code, 409)

        # 2. Rejected import replay under restricted role
        empty_pdf = bad_pdfs.build("empty")
        res_rej1 = client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-rej",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "restricted-rej-key-1",
            },
            content=empty_pdf,
        )
        self.assertEqual(res_rej1.status_code, 422)
        self.assertEqual(res_rej1.json()["error"]["code"], "empty_file")

        # Exact replay of rejection returns same error response without duplicate audit rows
        res_rej2 = client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-rej",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "restricted-rej-key-1",
            },
            content=empty_pdf,
        )
        self.assertEqual(res_rej2.status_code, 422)
        first_error, replay_error = res_rej1.json()["error"], res_rej2.json()["error"]
        self.assertEqual({k: v for k, v in replay_error.items() if k != "request_id"},
                         {k: v for k, v in first_error.items() if k != "request_id"})
        self.assertEqual(first_error["request_id"], res_rej1.headers["x-request-id"])
        self.assertEqual(replay_error["request_id"], res_rej2.headers["x-request-id"])
        self.assertNotEqual(first_error["request_id"], replay_error["request_id"])

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT count(*) AS cnt FROM audit_events WHERE project_id = %s AND action = 'document.rejected'",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 1)

    def test_restricted_role_rejection_and_audit_event(self) -> None:
        """Restricted application-role real PostgreSQL test for rejection recording and actor context."""
        client, role = self.make_restricted_app_client()

        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        owner_token = issue_actor_token(owner_id, self.secret)

        observed_actor: list[str | None] = []
        original_record_rejection = lab_db.ingest._record_rejection

        def spy_record_rejection(conn, **kwargs):
            row = conn.execute("SELECT current_setting('lab.actor_id', true)").fetchone()
            observed_actor.append(row[0] if row else None)
            return original_record_rejection(conn, **kwargs)

        corrupt_pdf = bad_pdfs.build("truncated")
        with patch("lab_db.ingest._record_rejection", side_effect=spy_record_rejection):
            res = client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-corrupt",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=corrupt_pdf,
            )

        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "corrupt_pdf")
        self.assertEqual(observed_actor, [str(owner_id)])

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM operations WHERE project_id = %s AND kind = 'document.import'",
                    (project_id,),
                )
                op_row = cur.fetchone()
                self.assertIsNotNone(op_row)
                self.assertEqual(op_row["status"], "failed")
                self.assertEqual(op_row["actor_id"], owner_id)

                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s AND action = 'document.rejected'",
                    (project_id,),
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row["actor_id"], owner_id)
                self.assertEqual(audit_row["outcome"], "failed")

    def test_restricted_role_rollback_on_failure(self) -> None:
        """Restricted application-role real PostgreSQL test for transaction rollback on failure."""
        client, role = self.make_restricted_app_client()

        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        owner_token = issue_actor_token(owner_id, self.secret)

        def failing_insert_pages(*args, **kwargs):
            raise psycopg.DatabaseError("simulated page insert failure under restricted role")

        with patch("lab_db.ingest._insert_pages", side_effect=failing_insert_pages):
            res = client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-restricted-rollback",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "restricted-rollback-key",
                },
                content=self.f01_1,
            )
            self.assertIn(res.status_code, (500, 503))

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM resource_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM operations WHERE project_id = %s AND status = 'intent'", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM idempotency_keys WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

    def test_actor_context_fails_closed_when_setting_fails(self) -> None:
        """Verify that failure to establish transaction-local actor setting fails closed and rolls back."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
        owner_token = issue_actor_token(owner_id, self.secret)

        orig_connect = connect

        def failing_connect(settings):
            real_conn = orig_connect(settings)
            orig_execute = real_conn.execute

            def guarded_execute(query, params=None):
                if isinstance(query, str) and "set_config('lab.actor_id'" in query:
                    raise psycopg.DatabaseError("simulated set_config failure inside transaction")
                return orig_execute(query, params)

            real_conn.execute = guarded_execute  # type: ignore[assignment]
            return real_conn

        with patch("lab_api.documents.connect", side_effect=failing_connect):
            res = self.client.post(
                f"/api/v1/documents?project_id={project_id}&logical_id=doc-fail-closed",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=self.f01_1,
            )
            self.assertIn(res.status_code, (500, 503))

        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT count(*) AS cnt FROM document_versions WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
                cur.execute("SELECT count(*) AS cnt FROM operations WHERE project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)


if __name__ == "__main__":
    unittest.main()
