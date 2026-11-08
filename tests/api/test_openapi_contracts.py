"""Runtime OpenAPI contract verification tests (SPEC 8, 16.4, R22, T31, T77).

Validates that:
1. Running FastAPI /openapi.json accurately describes currently implemented routes,
   schemas, parameters, security schemes, and configured runtime limits.
2. No credentials, tokens, or gold-set fields appear in schemas or examples.
3. Actual HTTP representative successes and errors (200, 201, 400, 401, 403, 404,
   409, 413, 415, 422 business rejection & framework validation, 503) validate
   cleanly against the exported OpenAPI JSON schemas via jsonschema.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from fixtures.generators import synthetic_pdf
from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.benchmarks import encode_cursor
from lab_api.config import load_settings
from lab_api.documents import get_authenticated_actor, get_db, get_store
from lab_api.main import API_VERSION, create_app
from lab_db.ingest import PageGeometry
from lab_db.repo import Decision, NotFound, PermissionDenied


class OpenApiSchemaStructureTests(unittest.TestCase):
    """Structural assertions on the generated OpenAPI 3.1 document."""

    @classmethod
    def setUpClass(cls) -> None:
        env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": "postgresql://lab:secret@127.0.0.1:1/lab",
            "LAB_ARTIFACT_ROOT": "/tmp/test-artifacts",
            "LAB_AUTH_SECRET": "test-auth-secret-1234",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            cls.settings = load_settings()
        cls.app = create_app(cls.settings, checks={})
        cls.client = TestClient(cls.app)
        res = cls.client.get("/openapi.json")
        assert res.status_code == 200
        cls.openapi = res.json()

    def test_openapi_version_and_metadata(self) -> None:
        self.assertEqual(self.openapi.get("openapi"), "3.1.0")
        info = self.openapi.get("info", {})
        self.assertEqual(info.get("version"), API_VERSION)
        desc = info.get("description", "")
        self.assertIn("max_pdf_bytes=", desc)
        self.assertIn("upload_concurrency_slots=", desc)

    def test_security_schemes_configured(self) -> None:
        sec = self.openapi.get("components", {}).get("securitySchemes", {})
        self.assertIn("BearerAuth", sec)
        self.assertEqual(sec["BearerAuth"].get("type"), "http")
        self.assertEqual(sec["BearerAuth"].get("scheme"), "bearer")

    def test_paths_contain_only_implemented_endpoints(self) -> None:
        paths = set(self.openapi.get("paths", {}).keys())
        expected_paths = {
            "/healthz",
            "/readyz",
            "/api/v1/documents",
            "/documents",
            "/api/v1/projects/{project_id}/documents",
            "/api/v1/documents/{document_id}/original",
            "/documents/{document_id}/original",
            "/api/v1/documents/{document_id}/download",
            "/documents/{document_id}/download",
            "/api/v1/documents/{document_id}",
            "/documents/{document_id}",
            "/api/v1/documents/{document_id}/pages/{page_index}",
            "/documents/{document_id}/pages/{page_index}",
            "/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions",
        }
        self.assertEqual(paths, expected_paths)

    def test_no_credentials_or_gold_fields_in_schema(self) -> None:
        serialized = json.dumps(self.openapi).lower()
        forbidden_keywords = [
            "password",
            "signing_secret",
            "secret_key",
            "gold_facts",
            "ground_truth",
            "evidence_sets",
        ]
        for kw in forbidden_keywords:
            with self.subTest(kw=kw):
                self.assertNotIn(kw, serialized)

    def test_every_ref_resolves_to_components_schema(self) -> None:
        schemas = self.openapi.get("components", {}).get("schemas", {})

        def collect_refs(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "$ref":
                        yield v
                    else:
                        yield from collect_refs(v)
            elif isinstance(node, list):
                for item in node:
                    yield from collect_refs(item)

        for ref in collect_refs(self.openapi):
            self.assertTrue(ref.startswith("#/components/schemas/"), f"Unexpected ref: {ref}")
            name = ref.rsplit("/", 1)[1]
            self.assertIn(name, schemas, f"Dangling schema reference: {ref}")


    def test_components_are_valid_json_schemas(self):
        for name, schema in self.openapi["components"]["schemas"].items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

    def test_configured_limits_and_unrelated_registered_method_preserved(self):
        app = create_app(self.settings, checks={})
        app.state.max_pdf_bytes = 12345
        app.state.max_pdf_pages = 21
        app.state.upload_read_timeout_s = 7

        @app.post("/api/v1/documents/{document_id}")
        def unrelated_operation(document_id: str):
            return {"id": document_id}

        schema = app.openapi()
        self.assertIn("max_pdf_bytes=12345", schema["info"]["description"])
        self.assertIn("max_pdf_pages=21", schema["info"]["description"])
        self.assertIn("upload_timeout=7s", schema["info"]["description"])
        self.assertIn("post", schema["paths"]["/api/v1/documents/{document_id}"])
        self.assertIn("get", schema["paths"]["/api/v1/documents/{document_id}"])


class RuntimeHttpContractTests(unittest.TestCase):
    """Validates real HTTP runtime responses against the exported OpenAPI schemas."""

    def setUp(self) -> None:
        self.env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": "postgresql://lab:secret@127.0.0.1:1/lab",
            "LAB_ARTIFACT_ROOT": "/tmp/test-artifacts",
            "LAB_AUTH_SECRET": "test-auth-secret-1234",
        }
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            self.settings = load_settings()

        self.app = create_app(self.settings, checks={})
        self.client = TestClient(self.app)
        self.openapi_doc = self.client.get("/openapi.json").json()

        # Build jsonschema registry
        resource = DRAFT202012.create_resource(self.openapi_doc)
        self.registry = Registry().with_resource("urn:openapi", resource)

        self.actor_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.doc_id = uuid.uuid4()
        self.signing_secret = get_signing_secret(self.settings)
        self.valid_token = issue_actor_token(self.actor_id, self.signing_secret)
        self.auth_headers = {"Authorization": f"Bearer {self.valid_token}"}

        self.mock_conn = MagicMock()
        self.mock_store = MagicMock()

        self.app.dependency_overrides[get_db] = lambda: self.mock_conn
        self.app.dependency_overrides[get_store] = lambda: self.mock_store
        self.app.dependency_overrides[get_authenticated_actor] = lambda: self.actor_id

    def _validate_schema(self, path: str, method: str, status_code: int, response_data: dict) -> None:
        """Validate JSON data against OpenAPI path operation response schema."""
        op = self.openapi_doc["paths"][path][method.lower()]
        resp_spec = op["responses"].get(str(status_code))
        self.assertIsNotNone(resp_spec, f"Status {status_code} not described in OpenAPI for {method.upper()} {path}")
        schema = resp_spec.get("content", {}).get("application/json", {}).get("schema")
        self.assertIsNotNone(schema, f"No JSON schema for {status_code} on {method.upper()} {path}")

        # Rewrite local refs to urn:openapi
        def rewrite_refs(node):
            if isinstance(node, dict):
                return {k: ("urn:openapi" + v if k == "$ref" and v.startswith("#") else rewrite_refs(v)) for k, v in node.items()}
            elif isinstance(node, list):
                return [rewrite_refs(item) for item in node]
            return node

        resolved_schema = rewrite_refs(schema)
        validator = Draft202012Validator(resolved_schema, registry=self.registry, format_checker=FormatChecker())
        errors = list(validator.iter_errors(response_data))
        if errors:
            self.fail(f"Schema validation failed for {method.upper()} {path} ({status_code}): {errors[0].message}")

    def test_healthz_runtime_contract(self) -> None:
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self._validate_schema("/healthz", "GET", 200, res.json())

    def test_readyz_runtime_contract_ready_and_not_ready(self) -> None:
        # Ready (200)
        from lab_api.readiness import CheckResult
        app_ready = create_app(self.settings, checks={"db": lambda: CheckResult(True, "ok")})
        c_ready = TestClient(app_ready)
        res_ready = c_ready.get("/readyz")
        self.assertEqual(res_ready.status_code, 200)
        self._validate_schema("/readyz", "GET", 200, res_ready.json())

        # Not ready (503)
        app_down = create_app(self.settings, checks={"db": lambda: CheckResult(False, "down")})
        c_down = TestClient(app_down)
        res_down = c_down.get("/readyz")
        self.assertEqual(res_down.status_code, 503)
        self._validate_schema("/readyz", "GET", 503, res_down.json())

    def test_upload_success_201_runtime_contract(self) -> None:
        pdf_bytes = synthetic_pdf.build(1)
        res_id = uuid.uuid4()
        art_id = uuid.uuid4()
        doc_ver_id = uuid.uuid4()

        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        self.mock_conn.transaction.side_effect = MockTx
        # Authorize returns None (success)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (res_id, art_id, 1),  # query committed row
        ]
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)), \
             unittest.mock.patch("lab_api.documents.ingest_document", return_value=doc_ver_id):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/pdf"},
                params={"project_id": str(self.project_id), "logical_id": "test-doc"},
                content=pdf_bytes,
            )

        self.assertEqual(res.status_code, 201)
        self._validate_schema("/api/v1/documents", "POST", 201, res.json())

    def test_upload_replay_200_runtime_contract(self) -> None:
        pdf_bytes = synthetic_pdf.build(1)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        req_hash = hashlib.sha256(f"default:{pdf_sha}".encode("utf-8")).hexdigest()
        op_id = uuid.uuid4()
        doc_ver_id = uuid.uuid4()

        stored_body = {
            "id": str(doc_ver_id),
            "document_version_id": str(doc_ver_id),
            "project_id": str(self.project_id),
            "resource_id": str(uuid.uuid4()),
            "artifact_id": str(uuid.uuid4()),
            "logical_id": "default",
            "revision": 1,
            "sha256": pdf_sha,
            "size_bytes": len(pdf_bytes),
            "page_count": 1,
            "flagged": False,
            "warnings": [],
        }

        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        self.mock_conn.transaction.side_effect = MockTx
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (req_hash, op_id, "succeeded"),  # existing idempotency key
            (201, stored_body),               # stored original
        ]
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            res = self.client.post(
                "/api/v1/documents",
                headers={
                    **self.auth_headers,
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "test-key-1",
                },
                params={"project_id": str(self.project_id)},
                content=pdf_bytes,
            )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("replayed"))
        self._validate_schema("/api/v1/documents", "POST", 200, res.json())

    def test_upload_bad_request_400_runtime_contract(self) -> None:
        res = self.client.post(
            "/api/v1/documents",
            headers={**self.auth_headers, "Content-Type": "application/pdf"},
            # missing project_id
            content=synthetic_pdf.build(1),
        )
        self.assertEqual(res.status_code, 400)
        self._validate_schema("/api/v1/documents", "POST", 400, res.json())

    def test_upload_unauthorized_401_runtime_contract(self) -> None:
        # Remove dependency override for get_authenticated_actor to trigger auth check
        del self.app.dependency_overrides[get_authenticated_actor]
        res = self.client.post(
            "/api/v1/documents",
            headers={"Content-Type": "application/pdf"},
            params={"project_id": str(self.project_id)},
            content=synthetic_pdf.build(1),
        )
        self.assertEqual(res.status_code, 401)
        self._validate_schema("/api/v1/documents", "POST", 401, res.json())

    def test_upload_permission_denied_403_runtime_contract(self) -> None:
        with unittest.mock.patch("lab_api.documents.authorize", side_effect=PermissionDenied("forbidden")):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/pdf"},
                params={"project_id": str(self.project_id)},
                content=synthetic_pdf.build(1),
            )
        self.assertEqual(res.status_code, 403)
        self._validate_schema("/api/v1/documents", "POST", 403, res.json())

    def test_upload_not_found_404_runtime_contract(self) -> None:
        with unittest.mock.patch("lab_api.documents.authorize", side_effect=NotFound("project not found")):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/pdf"},
                params={"project_id": str(self.project_id)},
                content=synthetic_pdf.build(1),
            )
        self.assertEqual(res.status_code, 404)
        self._validate_schema("/api/v1/documents", "POST", 404, res.json())

    def test_upload_conflict_409_runtime_contract(self) -> None:
        pdf_bytes = synthetic_pdf.build(1)

        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        self.mock_conn.transaction.side_effect = MockTx
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("different-hash", uuid.uuid4(), "succeeded")
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            res = self.client.post(
                "/api/v1/documents",
                headers={
                    **self.auth_headers,
                    "Content-Type": "application/pdf",
                    "Idempotency-Key": "conflict-key",
                },
                params={"project_id": str(self.project_id)},
                content=pdf_bytes,
            )
        self.assertEqual(res.status_code, 409)
        self._validate_schema("/api/v1/documents", "POST", 409, res.json())

    def test_upload_payload_too_large_413_runtime_contract(self) -> None:
        large_bytes = b"%PDF-1.7 " + b"0" * 2000
        self.app.state.max_pdf_bytes = 1000
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/pdf"},
                params={"project_id": str(self.project_id)},
                content=large_bytes,
            )
        self.assertEqual(res.status_code, 413)
        self._validate_schema("/api/v1/documents", "POST", 413, res.json())

    def test_upload_unsupported_media_type_415_runtime_contract(self) -> None:
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/json"},
                params={"project_id": str(self.project_id)},
                content=b"{}",
            )
        self.assertEqual(res.status_code, 415)
        self._validate_schema("/api/v1/documents", "POST", 415, res.json())

    def test_upload_business_rejection_422_runtime_contract(self) -> None:
        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        self.mock_conn.transaction.side_effect = MockTx
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            res = self.client.post(
                "/api/v1/documents",
                headers={**self.auth_headers, "Content-Type": "application/pdf"},
                params={"project_id": str(self.project_id)},
                content=b"not-a-valid-pdf",
            )
        self.assertEqual(res.status_code, 422)
        self._validate_schema("/api/v1/documents", "POST", 422, res.json())

    def test_framework_validation_error_422_runtime_contract(self) -> None:
        # Invalid project_id format in path
        res = self.client.post(
            "/api/v1/projects/not-a-uuid/documents",
            headers={**self.auth_headers, "Content-Type": "application/pdf"},
            content=synthetic_pdf.build(1),
        )
        self.assertEqual(res.status_code, 422)
        self.assertIn("detail", res.json())
        self._validate_schema("/api/v1/projects/{project_id}/documents", "POST", 422, res.json())

    def test_document_metadata_200_runtime_contract(self) -> None:
        valid_sha = hashlib.sha256(b"metadata-test").hexdigest()
        row = (
            self.doc_id, self.project_id, uuid.uuid4(), uuid.uuid4(),
            valid_sha, 5, "default", 1, valid_sha, 1024, "key", "committed",
        )
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = row
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("reviewer", None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}",
                headers=self.auth_headers,
            )
        self.assertEqual(res.status_code, 200)
        self._validate_schema("/api/v1/documents/{document_id}", "GET", 200, res.json())

    def test_document_download_200_binary_runtime_contract(self) -> None:
        pdf_bytes = synthetic_pdf.build(1)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        row = (
            self.doc_id, self.project_id, uuid.uuid4(), uuid.uuid4(),
            pdf_sha, 1, "default", 1, pdf_sha, len(pdf_bytes), "key", "committed",
        )
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = row
        self.mock_conn.execute.return_value = mock_cursor
        self.mock_store.open.return_value = pdf_bytes

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("reviewer", None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/original",
                headers=self.auth_headers,
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("content-type"), "application/pdf")
        self.assertEqual(res.content, pdf_bytes)

    def test_document_page_geometry_200_runtime_contract(self) -> None:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (self.project_id, 3),                 # doc row (project_id, page_count)
            (0, "1", 0, 612.0, 792.0),             # page row
        ]
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("reviewer", None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers=self.auth_headers,
            )
        self.assertEqual(res.status_code, 200)
        self._validate_schema("/api/v1/documents/{document_id}/pages/{page_index}", "GET", 200, res.json())

    def test_benchmark_public_questions_200_runtime_contract(self) -> None:
        bench_id = uuid.uuid4()
        qid = uuid.uuid4()
        q_row = (qid, "k1", 1, "What is the result?", "hash1", "bucket1", {"language": "en"})

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (bench_id, "frozen")
        mock_cursor.fetchall.return_value = [q_row]
        self.mock_conn.execute.return_value = mock_cursor

        with unittest.mock.patch("lab_api.benchmarks.authorize", return_value=Decision("reviewer", None)):
            res = self.client.get(
                "/api/v1/benchmarks/math-bench/revisions/1/questions",
                headers={**self.auth_headers, "X-Project-Id": str(self.project_id)},
            )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self._validate_schema("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions", "GET", 200, data)
        # Verify no gold fields in public items
        item = data["items"][0]
        self.assertEqual(set(item.keys()), {"id", "question_text", "language"})


    def _prepare_successful_upload(self):
        self.mock_conn.execute.return_value.fetchone.return_value = (uuid.uuid4(), uuid.uuid4(), 1)
        return unittest.mock.patch("lab_api.documents.ingest_document", return_value=uuid.uuid4())

    def test_multipart_aliases_and_custom_pdf_name_runtime_contract(self):
        path = "/api/v1/documents"
        schema = self.openapi_doc["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        schema = {"$ref": "urn:openapi" + schema["$ref"]}
        for field in ("file", "pdf", "document", "custom_upload"):
            with self.subTest(field=field), self._prepare_successful_upload(), unittest.mock.patch(
                "lab_api.documents.authorize", return_value=Decision("owner", None)
            ):
                # Binary form data has a string representation in OpenAPI;
                # MIME headers and exactly-one-PDF are validated by the parser.
                Draft202012Validator(schema, registry=self.registry).validate({field: "PDF bytes", "logical_id": "form-id"})
                response = self.client.post(path, params={"project_id": str(self.project_id)},
                    files={field: ("input.pdf", synthetic_pdf.build(1), "application/pdf")},
                    data={"logical_id": "form-id"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["logical_id"], "form-id")
                self._validate_schema(path, "post", 201, response.json())
                for required in ("flagged", "warnings"):
                    incomplete = dict(response.json())
                    del incomplete[required]
                    with self.assertRaises(AssertionError):
                        self._validate_schema(path, "post", 201, incomplete)

    def test_multipart_logical_id_raw_text_is_stripped_before_effective_limits(self):
        path = "/api/v1/documents"
        ref = self.openapi_doc["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        for raw, expected in (("", "default"), (" " * 130 + "A", "A")):
            with self.subTest(raw_length=len(raw)), self._prepare_successful_upload(), unittest.mock.patch(
                "lab_api.documents.authorize", return_value=Decision("owner", None)
            ):
                Draft202012Validator({"$ref": "urn:openapi" + ref}, registry=self.registry).validate(
                    {"file": "binary PDF", "logical_id": raw})
                response = self.client.post(path, params={"project_id": str(self.project_id)},
                    files={"file": ("input.pdf", synthetic_pdf.build(1), "application/pdf")}, data={"logical_id": raw})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["logical_id"], expected)
                self._validate_schema(path, "post", 201, response.json())

    def test_runtime_error_echoes_unbounded_request_id_and_message(self):
        path = "/api/v1/documents"
        for request_id in ("", "r" * 129):
            with self.subTest(request_id_length=len(request_id)):
                response = self.client.post(path, headers={"Content-Type": "application/pdf", "X-Request-Id": request_id}, content=b"pdf")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["request_id"], request_id)
                self._validate_schema(path, "post", 400, response.json())
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            response = self.client.post(path, params={"project_id": str(self.project_id)},
                files={"file": ("input.pdf", b"pdf", "a" * 2500)})
        self.assertEqual(response.status_code, 415)
        self.assertGreater(len(response.json()["error"]["message"]), 2000)
        self._validate_schema(path, "post", 415, response.json())

    def test_legacy_replay_without_optional_inspection_fields(self):
        pdf = synthetic_pdf.build(1)
        digest = hashlib.sha256(pdf).hexdigest()
        request_hash = hashlib.sha256(f"default:{digest}".encode()).hexdigest()
        self.mock_conn.execute.return_value.fetchone.side_effect = [
            (request_hash, uuid.uuid4(), "succeeded"), None,
            (self.doc_id, uuid.uuid4(), uuid.uuid4(), digest, 1, "default", 1, len(pdf)),
        ]
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)):
            response = self.client.post("/api/v1/documents", params={"project_id": str(self.project_id)},
                headers={"Content-Type": "application/pdf", "Idempotency-Key": "old-upload"}, content=pdf)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("warnings", response.json())
        self._validate_schema("/api/v1/documents", "post", 200, response.json())
        incomplete = dict(response.json())
        del incomplete["replayed"]
        with self.assertRaises(AssertionError):
            self._validate_schema("/api/v1/documents", "post", 200, incomplete)

    def test_pdf_content_negotiation_and_all_download_aliases(self):
        pdf = synthetic_pdf.build(1)
        digest = hashlib.sha256(pdf).hexdigest()
        self.mock_conn.execute.return_value.fetchone.return_value = (
            self.doc_id, self.project_id, uuid.uuid4(), uuid.uuid4(), digest, 1, "default", 1,
            digest, len(pdf), "key", "committed")
        self.mock_store.open.return_value = pdf
        for prefix in ("/documents", "/api/v1/documents"):
            for suffix in ("", "/original", "/download"):
                path = prefix + "/{document_id}" + suffix
                with self.subTest(path=path), unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("reviewer", None)):
                    response = self.client.get(path.replace("{document_id}", str(self.doc_id)), headers={"Accept": "application/pdf"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, pdf)
                self.assertEqual(response.headers["content-type"], "application/pdf")
                self.assertIn("application/pdf", self.openapi_doc["paths"][path]["get"]["responses"]["200"]["content"])

    def test_observer_denied_and_description_does_not_promise_access(self):
        digest = "a" * 64
        self.mock_conn.execute.return_value.fetchone.return_value = (
            self.doc_id, self.project_id, uuid.uuid4(), uuid.uuid4(), digest, 1, "default", 1,
            digest, 1, "key", "committed")
        for suffix in ("", "/original"):
            path = "/api/v1/documents/{document_id}" + suffix
            with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("observer", "explicit_grant")):
                response = self.client.get(path.replace("{document_id}", str(self.doc_id)))
            self.assertEqual(response.status_code, 403)
            self._validate_schema(path, "get", 403, response.json())
            self.assertIn("Observer is denied", self.openapi_doc["paths"][path]["get"]["description"])

    def test_handled_auth_configuration_500_and_upload_admission_503(self):
        from lab_api.auth import AuthError
        del self.app.dependency_overrides[get_authenticated_actor]
        with unittest.mock.patch("lab_api.documents.get_signing_secret", side_effect=AuthError(500, "auth_not_configured", "authentication secret is not configured")):
            response = self.client.post("/api/v1/documents", headers={"Authorization": "Bearer synthetic"})
        self.assertEqual(response.status_code, 500)
        self._validate_schema("/api/v1/documents", "post", 500, response.json())
        self.app.dependency_overrides[get_authenticated_actor] = lambda: self.actor_id
        from lab_api.documents import ApiError
        with unittest.mock.patch("lab_api.documents.authorize", return_value=Decision("owner", None)), unittest.mock.patch(
            "lab_api.documents._take_slot", side_effect=ApiError(503, "server_busy", "too many uploads", retryable=True)
        ):
            response = self.client.post("/api/v1/documents", params={"project_id": str(self.project_id)}, content=b"pdf")
        self.assertEqual(response.status_code, 503)
        self._validate_schema("/api/v1/documents", "post", 503, response.json())

    def test_public_response_schema_rejects_extra_gold_and_invalid_uuid(self):
        path = "/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions"
        valid = {"benchmark_revision_id": str(uuid.uuid4()), "status": "frozen", "items": [
            {"id": str(uuid.uuid4()), "question_text": "Question?", "language": "en"}], "next_cursor": None}
        self._validate_schema(path, "get", 200, valid)
        for mutate in (
            lambda data: data.pop("next_cursor"),
            lambda data: data["items"][0].update({"reference_answer": "hidden"}),
            lambda data: data["items"][0].update({"id": "not-a-uuid"}),
            lambda data: data["items"][0].update({"language": "not valid"}),
        ):
            data = json.loads(json.dumps(valid))
            mutate(data)
            with self.assertRaises(AssertionError):
                self._validate_schema(path, "get", 200, data)


if __name__ == "__main__":
    unittest.main()
