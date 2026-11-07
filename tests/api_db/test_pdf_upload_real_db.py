"""Real PostgreSQL integration tests for authenticated PDF upload and download (SPEC R01, 8, 10.1-10.2, 16.4, T01-T04, #67)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
import uuid

import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

# Ensure db, services/api, services/parser, contracts are available
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_db.lineage import walk
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
        """Concurrency test: 8 simultaneous uploads with the same Idempotency-Key create exactly 1 document version."""
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

    def test_invalid_page_geometry_rejection_and_atomic_replay_in_real_db(self) -> None:
        """Invalid page geometry persists failure without outbox; replay returns original 422, changed body is 409."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            reviewer_id = self.add_member(conn, project_id, role="reviewer")
            _, other_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        rev_token = issue_actor_token(reviewer_id, self.secret)
        other_token = issue_actor_token(other_id, self.secret)

        def failing_extract_pages(*args, **kwargs):
            raise ValueError("corrupted font dictionary / invalid page geometry")

        with patch("lab_api.documents.extract_pages", side_effect=failing_extract_pages):
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
            self.assertEqual(res_bad_geom.json()["error"]["code"], "invalid_page_geometry")
            self.assertEqual(
                res_bad_geom.json()["error"]["message"],
                "failed to parse page geometry from PDF",
            )
            self.assertEqual(
                res_bad_geom.json()["error"]["details"],
                {"error": "ValueError"},
            )

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
                self.assertEqual(audit_row["reason"], "invalid_page_geometry")

                cur.execute(
                    "SELECT count(*) AS cnt FROM outbox_events WHERE project_id = %s",
                    (project_id,),
                )
                self.assertEqual(cur.fetchone()["cnt"], 0)

        # 1. Identical request with same Idempotency-Key replays original 422 and exact response
        with patch("lab_api.documents.extract_pages", side_effect=failing_extract_pages):
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
            self.assertEqual(res_replay.json()["error"]["code"], "invalid_page_geometry")
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


if __name__ == "__main__":
    unittest.main()
