"""Unit and boundary tests for authenticated PDF upload and download API endpoints (SPEC 8, 16.4, T02, T03, T04)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import Settings, load_settings
from lab_api.documents import get_authenticated_actor, get_db, get_store
from lab_api.main import create_app
from lab_db.storage import LocalArtifactStore
from fixtures.generators import bad_pdfs, synthetic_pdf

ENV = {
    "LAB_ENV": "test",
    "LAB_PROVIDER_MODE": "mock",
    "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@127.0.0.1:1/lab",
    "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts",
    "LAB_DB_CONNECT_TIMEOUT_S": "2",
}


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self._idx = 0

    def fetchone(self) -> Any:
        if self._rows and self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self) -> list[Any]:
        return self._rows


class MockConnection:
    """In-memory mock connection simulating PostgreSQL state for API boundary tests."""

    def __init__(self, artifact_store: LocalArtifactStore) -> None:
        self.store = artifact_store
        self.actors: set[uuid.UUID] = set()
        self.projects: set[uuid.UUID] = set()
        self.project_members: dict[tuple[uuid.UUID, uuid.UUID], str] = {}  # (proj, actor) -> role
        self.document_versions: dict[uuid.UUID, dict[str, Any]] = {}
        self.resource_versions: dict[uuid.UUID, dict[str, Any]] = {}
        self.pages: dict[tuple[uuid.UUID, int], dict[str, Any]] = {}
        self.artifacts: dict[uuid.UUID, dict[str, Any]] = {}
        self.idempotency_keys: dict[tuple[uuid.UUID, str, str], tuple[str, uuid.UUID]] = {}
        self.operations: dict[uuid.UUID, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []

    def transaction(self):
        return FakeTransaction()

    def execute(self, query: str, params: tuple = ()) -> FakeCursor:
        q = " ".join(query.strip().split())

        if "SELECT 1 FROM actors WHERE id = %s" in q:
            actor_id = params[0]
            if actor_id in self.actors:
                return FakeCursor([(1,)])
            return FakeCursor([])

        if "SELECT role FROM project_members WHERE project_id = %s AND actor_id = %s" in q:
            proj_id, actor_id = params
            role = self.project_members.get((proj_id, actor_id))
            if role is not None:
                return FakeCursor([(role,)])
            return FakeCursor([])

        if "SELECT ik.request_hash, ik.operation_id, op.status FROM idempotency_keys ik" in q:
            proj_id = params[0]
            key = params[-1]
            scope = "document.import"
            for (p_id, s_scope, k_key), (req_hash, op_id) in self.idempotency_keys.items():
                if p_id == proj_id and s_scope == scope and k_key == key:
                    op = self.operations.get(op_id, {})
                    op_status = op.get("status", "intent")
                    return FakeCursor([(req_hash, op_id, op_status)])
            return FakeCursor([])

        if "SELECT dv.id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count" in q and "FROM audit_events ae" in q:
            proj_id, op_id = params
            for ae in self.audit_events:
                if ae.get("project_id") == proj_id and ae.get("operation_id") == op_id:
                    res_id = ae.get("resource_id")
                    for dv_id, dv in self.document_versions.items():
                        if dv.get("resource_id") == res_id:
                            rv = self.resource_versions.get(res_id, {})
                            return FakeCursor([(
                                dv_id, dv["resource_id"], dv["artifact_id"], dv["sha256"],
                                dv["page_count"], rv.get("logical_id", "default"), rv.get("revision", 1)
                            )])
            return FakeCursor([])

        if "SELECT set_config" in q or "SELECT pg_advisory_xact_lock" in q:
            return FakeCursor([])

        if "INSERT INTO operations" in q:
            if len(params) == 5:
                op_id, p_id, kind, a_id, status = params
            elif len(params) == 3:
                op_id, p_id, a_id = params
                kind = "document.import"
                status = "failed"
            else:
                op_id = params[0]
                p_id = params[1]
                kind = "document.import"
                status = "intent"
                a_id = params[-1]
            self.operations[op_id] = {
                "id": op_id,
                "project_id": p_id,
                "kind": kind,
                "actor_id": a_id,
                "status": status,
            }
            return FakeCursor([])

        if "INSERT INTO audit_events" in q or "UPDATE operations" in q:
            return FakeCursor([])

        if "INSERT INTO idempotency_keys" in q:
            p_id, scope, key, req_hash, op_id = params
            self.idempotency_keys[(p_id, scope, key)] = (req_hash, op_id)
            return FakeCursor([])

        if "UPDATE idempotency_keys SET operation_id = %s" in q:
            new_op_id, p_id, scope, key = params
            if (p_id, scope, key) in self.idempotency_keys:
                req_h, _ = self.idempotency_keys[(p_id, scope, key)]
                self.idempotency_keys[(p_id, scope, key)] = (req_h, new_op_id)
            return FakeCursor([])

        if "SELECT ae.operation_id FROM document_versions dv" in q:
            doc_id = params[0]
            dv = self.document_versions.get(doc_id)
            if dv:
                res_id = dv.get("resource_id")
                for ae in self.audit_events:
                    if ae.get("resource_id") == res_id:
                        return FakeCursor([(ae.get("operation_id"),)])
            return FakeCursor([])

        if "SELECT dv.resource_id, dv.artifact_id, rv.revision FROM document_versions dv" in q:
            doc_id = params[0]
            dv = self.document_versions.get(doc_id)
            if dv:
                rv = self.resource_versions.get(dv["resource_id"], {})
                return FakeCursor([(dv["resource_id"], dv["artifact_id"], rv.get("revision", 1))])
            return FakeCursor([])

        if "SELECT dv.project_id, dv.sha256, a.object_key, a.size_bytes FROM document_versions dv" in q:
            doc_id = params[0]
            dv = self.document_versions.get(doc_id)
            if dv:
                art = self.artifacts.get(dv["artifact_id"])
                if art:
                    return FakeCursor([(dv["project_id"], dv["sha256"], art["object_key"], art["size_bytes"])])
            return FakeCursor([])

        if "SELECT dv.id, dv.project_id, dv.resource_id, dv.artifact_id, dv.sha256, dv.page_count, rv.logical_id, rv.revision, a.size_bytes, a.object_key" in q:
            doc_id = params[0]
            dv = self.document_versions.get(doc_id)
            if dv:
                rv = self.resource_versions.get(dv["resource_id"], {})
                art = self.artifacts.get(dv["artifact_id"], {})
                return FakeCursor([(
                    dv["id"], dv["project_id"], dv["resource_id"], dv["artifact_id"],
                    dv["sha256"], dv["page_count"], rv.get("logical_id", "default"), rv.get("revision", 1),
                    art.get("size_bytes", 0), art.get("object_key", "")
                )])
            return FakeCursor([])

        if "SELECT project_id, page_count FROM document_versions WHERE id = %s" in q:
            doc_id = params[0]
            dv = self.document_versions.get(doc_id)
            if dv:
                return FakeCursor([(dv["project_id"], dv["page_count"])])
            return FakeCursor([])

        if "SELECT page_index, page_label, rotation, width_pt, height_pt FROM pages" in q:
            doc_id, p_idx = params
            page = self.pages.get((doc_id, p_idx))
            if page:
                return FakeCursor([(page["page_index"], page["page_label"], page["rotation"], page["width_pt"], page["height_pt"])])
            return FakeCursor([])

        # Fallback empty cursor
        return FakeCursor([])


class DocumentsApiTests(unittest.TestCase):
    """Test suite for document upload, download, boundaries and security."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifact_root = Path(self.tmp.name).resolve()
        self.store = LocalArtifactStore(self.artifact_root)

        with mock.patch.dict(os.environ, {**ENV, "LAB_ARTIFACT_ROOT": str(self.artifact_root)}, clear=True):
            self.settings = load_settings()

        self.app = create_app(settings=self.settings, checks={})
        self.conn = MockConnection(self.store)

        # Setup test project and actors
        self.project_id = uuid.uuid4()
        self.owner_id = uuid.uuid4()
        self.reviewer_id = uuid.uuid4()
        self.observer_id = uuid.uuid4()
        self.other_project_id = uuid.uuid4()

        self.conn.projects.add(self.project_id)
        self.conn.projects.add(self.other_project_id)
        self.conn.actors.add(self.owner_id)
        self.conn.actors.add(self.reviewer_id)
        self.conn.actors.add(self.observer_id)

        self.conn.project_members[(self.project_id, self.owner_id)] = "owner"
        self.conn.project_members[(self.project_id, self.reviewer_id)] = "reviewer"
        self.conn.project_members[(self.project_id, self.observer_id)] = "observer"
        self.conn.project_members[(self.other_project_id, self.owner_id)] = "owner"

        self.secret = get_signing_secret(self.settings)
        self.owner_token = issue_actor_token(self.owner_id, self.secret)
        self.reviewer_token = issue_actor_token(self.reviewer_id, self.secret)
        self.observer_token = issue_actor_token(self.observer_id, self.secret)

        self.app.dependency_overrides[get_db] = lambda: self.conn
        self.app.dependency_overrides[get_store] = lambda: self.store
        self.client = TestClient(self.app)

        self.f01_21 = synthetic_pdf.build(21)
        self.f01_sha256 = hashlib.sha256(self.f01_21).hexdigest()

    def test_upload_missing_auth_header_rejected_401(self) -> None:
        """Missing Authorization header is rejected with 401."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={"Content-Type": "application/pdf"},
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "unauthorized")

    def test_upload_caller_asserted_actor_id_rejected_401(self) -> None:
        """Raw actor UUID in Bearer header (caller-asserted identity) is rejected with 401."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_id}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "unauthorized")

    def test_upload_x_actor_id_header_rejected_401(self) -> None:
        """X-Actor-Id header without trusted token is rejected with 401."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "X-Actor-Id": str(self.owner_id),
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "unauthorized")

    def test_upload_tampered_token_rejected_401(self) -> None:
        """Tampered token with invalid signature is rejected with 401."""
        tampered_token = f"lab_act_{self.owner_id}_badsignature1234567890abcdef"
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {tampered_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "unauthorized")

    def test_upload_signed_token_for_unknown_actor_rejected_401(self) -> None:
        """Actor UUID not present in actors table is rejected with 401 even with valid signature."""
        unknown_actor = uuid.uuid4()
        unknown_token = issue_actor_token(unknown_actor, self.secret)
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {unknown_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "unauthorized")

    def test_upload_missing_project_id_rejected_400(self) -> None:
        """Missing project_id is rejected with 400."""
        res = self.client.post(
            "/api/v1/documents",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "bad_request")

    def test_upload_actor_not_in_project_rejected_404(self) -> None:
        """Actor trying to upload to an unknown/inaccessible project gets 404 (never reveal existence)."""
        unknown_project = uuid.uuid4()
        res = self.client.post(
            f"/api/v1/documents?project_id={unknown_project}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "not_found")

    def test_upload_unauthorized_role_rejected_403(self) -> None:
        """Actor with reviewer role is rejected with 403 (SPEC 16.4 role matrix)."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.reviewer_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "permission_denied")

    def test_upload_wrong_content_type_rejected_415(self) -> None:
        """Wrong Content-Type (text/plain, application/json) is rejected with 415."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "text/plain",
            },
            content=b"some plain text",
        )
        self.assertEqual(res.status_code, 415)
        self.assertEqual(res.json()["error"]["code"], "invalid_content_type")

    def test_upload_server_owned_byte_limit_enforced_413(self) -> None:
        """Payload exceeding server-owned max_pdf_bytes limit is rejected with 413."""
        self.app.state.max_pdf_bytes = len(self.f01_21) - 1
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["error"]["code"], "file_too_large")
        # Reset limit
        delattr(self.app.state, "max_pdf_bytes")

    def test_caller_query_param_cannot_raise_server_limit(self) -> None:
        """Query parameters ?max_bytes=... cannot raise or bypass server-owned limits."""
        self.app.state.max_pdf_bytes = len(self.f01_21) - 1
        # Client tries to pass ?max_bytes=99999999 to raise limit
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}&max_bytes=99999999",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["error"]["code"], "file_too_large")
        delattr(self.app.state, "max_pdf_bytes")

    def test_upload_f05_empty_file_rejected_422(self) -> None:
        """Empty PDF file (F05.empty) is rejected with 422 empty_file."""
        empty_data = bad_pdfs.build("empty")
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=empty_data,
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "empty_file")

    def test_upload_f05_fake_text_rejected_422(self) -> None:
        """Fake text without %PDF- header (F05.fake_text) is rejected with 422 invalid_pdf."""
        fake_data = bad_pdfs.build("fake_text")
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=fake_data,
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "invalid_pdf")

    def test_upload_f05_corrupt_pdf_rejected_422(self) -> None:
        """Corrupt truncated PDF (F05.truncated) is rejected with 422 corrupt_pdf."""
        corrupt_data = bad_pdfs.build("truncated")
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=corrupt_data,
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "corrupt_pdf")

    def test_upload_f05_encrypted_pdf_rejected_422(self) -> None:
        """Encrypted PDF (F05.encrypted) is rejected with 422 unsupported_encryption."""
        encrypted_data = bad_pdfs.build("encrypted")
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=encrypted_data,
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "unsupported_encryption")

    def test_upload_server_owned_page_limit_enforced_422(self) -> None:
        """Exceeding server-owned page limit is rejected with 422 page_limit_exceeded."""
        self.app.state.max_pdf_pages = 20  # F01 has 21 pages
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "page_limit_exceeded")
        delattr(self.app.state, "max_pdf_pages")

    def test_upload_download_round_trip_success(self) -> None:
        """Valid upload persists and download returns exact raw PDF bytes."""
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()

        with mock.patch("lab_api.documents.ingest_document", return_value=doc_id):
            stored = self.store.put(self.f01_21)
            self.conn.artifacts[art_id] = {
                "object_key": stored.object_key,
                "size_bytes": stored.size_bytes,
                "sha256": self.f01_sha256,
            }
            self.conn.resource_versions[res_id] = {
                "logical_id": "f01-test",
                "revision": 1,
            }
            self.conn.document_versions[doc_id] = {
                "id": doc_id,
                "project_id": self.project_id,
                "resource_id": res_id,
                "artifact_id": art_id,
                "sha256": self.f01_sha256,
                "page_count": 21,
            }
            for i in range(21):
                self.conn.pages[(doc_id, i)] = {
                    "page_index": i,
                    "page_label": f"p-{i + 1}",
                    "rotation": 0,
                    "width_pt": 595.0,
                    "height_pt": 842.0,
                }

            upload_res = self.client.post(
                f"/api/v1/documents?project_id={self.project_id}&logical_id=f01-test",
                headers={
                    "Authorization": f"Bearer {self.owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=self.f01_21,
            )
            self.assertEqual(upload_res.status_code, 201)
            body = upload_res.json()
            self.assertEqual(body["id"], str(doc_id))
            self.assertEqual(body["sha256"], self.f01_sha256)
            self.assertEqual(body["page_count"], 21)

            # Download original PDF via /api/v1/documents/{id}/original
            dl_res = self.client.get(
                f"/api/v1/documents/{doc_id}/original",
                headers={"Authorization": f"Bearer {self.owner_token}"},
            )
            self.assertEqual(dl_res.status_code, 200)
            self.assertEqual(dl_res.headers["Content-Type"], "application/pdf")
            self.assertEqual(dl_res.content, self.f01_21)
            self.assertEqual(hashlib.sha256(dl_res.content).hexdigest(), self.f01_sha256)

            # Download via /api/v1/documents/{id} with Accept: application/pdf
            dl_accept = self.client.get(
                f"/api/v1/documents/{doc_id}",
                headers={
                    "Authorization": f"Bearer {self.owner_token}",
                    "Accept": "application/pdf",
                },
            )
            self.assertEqual(dl_accept.status_code, 200)
            self.assertEqual(dl_accept.content, self.f01_21)

            # Metadata query
            meta_res = self.client.get(
                f"/api/v1/documents/{doc_id}",
                headers={"Authorization": f"Bearer {self.owner_token}"},
            )
            self.assertEqual(meta_res.status_code, 200)
            meta = meta_res.json()
            self.assertEqual(meta["page_count"], 21)
            self.assertEqual(meta["logical_id"], "f01-test")

            # Page query
            p0_res = self.client.get(
                f"/api/v1/documents/{doc_id}/pages/0",
                headers={"Authorization": f"Bearer {self.owner_token}"},
            )
            self.assertEqual(p0_res.status_code, 200)
            p0 = p0_res.json()
            self.assertEqual(p0["page_index"], 0)
            self.assertEqual(p0["page_label"], "p-1")
            self.assertEqual(p0["width_pt"], 595.0)

            # Page out of bounds query -> 404
            p99_res = self.client.get(
                f"/api/v1/documents/{doc_id}/pages/99",
                headers={"Authorization": f"Bearer {self.owner_token}"},
            )
            self.assertEqual(p99_res.status_code, 404)

    def test_download_cross_project_rejected_404(self) -> None:
        """Actor from another project cannot download document from a project they don't belong to."""
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()

        stored = self.store.put(self.f01_21)
        self.conn.artifacts[art_id] = {
            "object_key": stored.object_key,
            "size_bytes": stored.size_bytes,
            "sha256": self.f01_sha256,
        }
        self.conn.resource_versions[res_id] = {"logical_id": "doc-proj-a", "revision": 1}
        self.conn.document_versions[doc_id] = {
            "id": doc_id,
            "project_id": self.project_id,
            "resource_id": res_id,
            "artifact_id": art_id,
            "sha256": self.f01_sha256,
            "page_count": 21,
        }

        # Actor from a different project who is NOT member of self.project_id
        outsider_actor = uuid.uuid4()
        self.conn.actors.add(outsider_actor)
        self.conn.project_members[(self.other_project_id, outsider_actor)] = "owner"
        outsider_token = issue_actor_token(outsider_actor, self.secret)

        res = self.client.get(
            f"/api/v1/documents/{doc_id}/original",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "not_found")

    def test_observer_without_grant_rejected_403(self) -> None:
        """Observer role without explicit grant is rejected with 403 on download."""
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()

        stored = self.store.put(self.f01_21)
        self.conn.artifacts[art_id] = {
            "object_key": stored.object_key,
            "size_bytes": stored.size_bytes,
            "sha256": self.f01_sha256,
        }
        self.conn.resource_versions[res_id] = {"logical_id": "doc-obs", "revision": 1}
        self.conn.document_versions[doc_id] = {
            "id": doc_id,
            "project_id": self.project_id,
            "resource_id": res_id,
            "artifact_id": art_id,
            "sha256": self.f01_sha256,
            "page_count": 21,
        }

        res = self.client.get(
            f"/api/v1/documents/{doc_id}/original",
            headers={"Authorization": f"Bearer {self.observer_token}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "permission_denied")

    def test_upload_missing_content_type_rejected_415(self) -> None:
        """Missing Content-Type header is rejected with 415."""
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={"Authorization": f"Bearer {self.owner_token}"},
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 415)
        self.assertEqual(res.json()["error"]["code"], "invalid_content_type")

    def test_upload_f05_bad_xref_flagged_success(self) -> None:
        """F05.bad_xref with repairable xref table is accepted with flagged=True and warnings."""
        bad_xref_data = bad_pdfs.build("bad_xref")
        bad_xref_sha = hashlib.sha256(bad_xref_data).hexdigest()
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()

        with mock.patch("lab_api.documents.ingest_document", return_value=doc_id):
            stored = self.store.put(bad_xref_data)
            self.conn.artifacts[art_id] = {
                "object_key": stored.object_key,
                "size_bytes": stored.size_bytes,
                "sha256": bad_xref_sha,
            }
            self.conn.resource_versions[res_id] = {
                "logical_id": "bad-xref-doc",
                "revision": 1,
            }
            self.conn.document_versions[doc_id] = {
                "id": doc_id,
                "project_id": self.project_id,
                "resource_id": res_id,
                "artifact_id": art_id,
                "sha256": bad_xref_sha,
                "page_count": 2,
            }

            res = self.client.post(
                f"/api/v1/documents?project_id={self.project_id}&logical_id=bad-xref-doc",
                headers={
                    "Authorization": f"Bearer {self.owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=bad_xref_data,
            )
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertTrue(body["flagged"])
            self.assertTrue(any("repaired" in w for w in body["warnings"]))

    def test_upload_f05_injection_accepted_as_pure_data(self) -> None:
        """F05.injection containing adversarial prompts is accepted as pure data without execution."""
        injection_data = bad_pdfs.build("injection")
        injection_sha = hashlib.sha256(injection_data).hexdigest()
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()

        with mock.patch("lab_api.documents.ingest_document", return_value=doc_id):
            stored = self.store.put(injection_data)
            self.conn.artifacts[art_id] = {
                "object_key": stored.object_key,
                "size_bytes": stored.size_bytes,
                "sha256": injection_sha,
            }
            self.conn.resource_versions[res_id] = {
                "logical_id": "injection-doc",
                "revision": 1,
            }
            self.conn.document_versions[doc_id] = {
                "id": doc_id,
                "project_id": self.project_id,
                "resource_id": res_id,
                "artifact_id": art_id,
                "sha256": injection_sha,
                "page_count": 1,
            }

            res = self.client.post(
                f"/api/v1/documents?project_id={self.project_id}&logical_id=injection-doc",
                headers={
                    "Authorization": f"Bearer {self.owner_token}",
                    "Content-Type": "application/pdf",
                },
                content=injection_data,
            )
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertFalse(body["flagged"])
            self.assertEqual(body["page_count"], 1)

    def test_upload_idempotency_same_key_same_body_replayed(self) -> None:
        """Submitting the same Idempotency-Key and body replays the previous document version."""
        doc_id = uuid.uuid4()
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()
        op_id = uuid.uuid4()

        stored = self.store.put(self.f01_21)
        self.conn.artifacts[art_id] = {
            "object_key": stored.object_key,
            "size_bytes": stored.size_bytes,
            "sha256": self.f01_sha256,
        }
        self.conn.resource_versions[res_id] = {
            "logical_id": "idemp-doc",
            "revision": 1,
        }
        self.conn.document_versions[doc_id] = {
            "id": doc_id,
            "project_id": self.project_id,
            "resource_id": res_id,
            "artifact_id": art_id,
            "sha256": self.f01_sha256,
            "page_count": 21,
        }
        self.conn.operations[op_id] = {
            "id": op_id,
            "project_id": self.project_id,
            "kind": "document.import",
            "actor_id": self.owner_id,
            "status": "succeeded",
        }
        self.conn.idempotency_keys[(self.project_id, "document.import", "k-replay-1")] = (
            self.f01_sha256,
            op_id,
        )
        self.conn.audit_events.append({
            "project_id": self.project_id,
            "operation_id": op_id,
            "resource_id": res_id,
        })

        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}&logical_id=idemp-doc",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "k-replay-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["replayed"])
        self.assertEqual(body["id"], str(doc_id))
        self.assertEqual(body["sha256"], self.f01_sha256)

    def test_upload_idempotency_same_key_different_body_conflict_409(self) -> None:
        """Submitting the same Idempotency-Key with different payload raises 409 conflict."""
        op_id = uuid.uuid4()
        other_sha = hashlib.sha256(b"other-content").hexdigest()
        self.conn.operations[op_id] = {
            "id": op_id,
            "project_id": self.project_id,
            "kind": "document.import",
            "actor_id": self.owner_id,
            "status": "succeeded",
        }
        self.conn.idempotency_keys[(self.project_id, "document.import", "k-conflict-1")] = (
            other_sha,
            op_id,
        )

        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": "k-conflict-1",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "conflict")

    def test_upload_idempotency_invalid_key_length_400(self) -> None:
        """Idempotency-Key exceeding 256 characters is rejected with 400."""
        too_long_key = "k" * 257
        res = self.client.post(
            f"/api/v1/documents?project_id={self.project_id}",
            headers={
                "Authorization": f"Bearer {self.owner_token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": too_long_key,
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
