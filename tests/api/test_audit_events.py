"""GET /audit-events without a database: filter validation, cursor, query shape."""
import base64
import datetime
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from lab_api import audit as a
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

P = uuid.uuid4()
T = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)


class FilterTests(unittest.TestCase):
    def test_valid_filters_are_typed(self):
        u = uuid.uuid4()
        f = a.parse_filters({"project_id": str(P), "actor_id": str(u), "resource_id": str(u), "resource_type": "document_version",
                             "action": "document.imported", "outcome": "denied",
                             "since": "2026-01-01T00:00:00+00:00", "until": "2026-01-02T00:00:00Z"})
        self.assertEqual(f["actor_id"], u)
        self.assertEqual(f["outcome"], "denied")
        self.assertEqual(f["since"].tzinfo is not None, True)
        self.assertEqual(a.parse_filters({"project_id": str(P)}), {})

    def test_bad_filters_are_400(self):
        bad = [{"actor_id": "x"}, {"resource_id": "1"}, {"resource_type": "Bad Type"}, {"action": "DROP;"},
               {"action": "a" * 70}, {"outcome": "maybe"}, {"since": "yesterday"}, {"until": "2026-01-01T00:00:00"},
               {"since": "2026-01-02T00:00:00+00:00", "until": "2026-01-01T00:00:00+00:00"},
               {"since": "2026-01-01T00:00:00+00:00", "until": "2026-01-01T00:00:00+00:00"},
               {"unknown": "1"}, {"actorid": str(uuid.uuid4())}]
        for params in bad:
            with self.subTest(params=params):
                with self.assertRaises(ApiError) as ctx:
                    a.parse_filters(params)
                self.assertEqual(ctx.exception.status_code, 400)


class CursorAndLimitTests(unittest.TestCase):
    def test_cursor_round_trip_and_binding(self):
        self.assertEqual(a.decode_cursor(a.encode_cursor(P, 9), P), 9)
        with self.assertRaises(ApiError):
            a.decode_cursor(a.encode_cursor(uuid.uuid4(), 9), P)
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode()
        for c in ("", "!!", enc([1]), enc({"p": str(P)}), enc({"p": str(P), "i": 0}), enc({"p": str(P), "i": True}),
                  enc({"p": str(P), "i": "3"}), enc({"p": str(P), "i": 3, "x": 1}), "A" * 300):
            with self.subTest(c=c[:12]):
                with self.assertRaises(ApiError) as ctx:
                    a.decode_cursor(c, P)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_cursor_strict_contract_binding_and_rejections(self):
        f1 = {"action": "document.imported"}
        f2 = {"action": "benchmark.frozen"}
        c_f1 = a.encode_cursor(P, 9, filters=f1)

        # Changed filters rejected
        for bad_filt in (f2, {}, None):
            with self.subTest(bad_filt=bad_filt):
                with self.assertRaises(ApiError) as ctx:
                    a.decode_cursor(c_f1, P, filters=bad_filt)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

        c_nofilt = a.encode_cursor(P, 9)
        with self.assertRaises(ApiError) as ctx:
            a.decode_cursor(c_nofilt, P, filters=f1)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Changed project rejected
        P2 = uuid.uuid4()
        with self.assertRaises(ApiError) as ctx:
            a.decode_cursor(c_f1, P2, filters=f1)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Changed endpoint rejected
        c_logs = a.encode_cursor(P, 9, endpoint="logs", filters=f1)
        with self.assertRaises(ApiError) as ctx:
            a.decode_cursor(c_logs, P, endpoint="audit-events", filters=f1)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Noncanonical base64 suffixes rejected
        for sfx in ("=", "==", "===", "A", " ", "\n"):
            with self.subTest(suffix=sfx):
                with self.assertRaises(ApiError) as ctx:
                    a.decode_cursor(c_f1 + sfx, P, filters=f1)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Explicit empty input rejected
        with self.assertRaises(ApiError) as ctx:
            a.decode_cursor("", P)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Malformed JSON and types
        for bad_obj in (
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_desc", "i": 0},
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_desc", "i": -1},
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_desc", "i": "9"},
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_desc", "i": True},
            {"p": str(P), "e": "audit-events", "f": "not-dict", "o": "id_desc", "i": 9},
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_asc", "i": 9},
            {"p": str(P), "e": "audit-events", "f": {}, "o": "id_desc"},
        ):
            raw_b64 = base64.urlsafe_b64encode(json.dumps(bad_obj).encode()).decode().rstrip("=")
            with self.subTest(bad=bad_obj):
                with self.assertRaises(ApiError) as ctx:
                    a.decode_cursor(raw_b64, P)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_limit(self):
        self.assertEqual(a.parse_limit(None), 50)
        self.assertEqual(a.parse_limit("200"), 200)
        for bad in ("0", "201", "-1", "x", "", "1.0", "٣", "9" * 5000, "999999", "000000"):
            with self.subTest(bad=bad):
                with self.assertRaises(ApiError) as ctx:
                    a.parse_limit(bad)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual(ctx.exception.code, "bad_request")


class QueryTests(unittest.TestCase):
    def run_list(self, filters=None, cursor=None, limit=2, rows=()):
        c = MagicMock()
        c.execute.return_value.fetchall.return_value = list(rows)
        with patch.object(a, "authorize", return_value=Decision("owner", None)):
            out = a._list_events(c, uuid.uuid4(), P, filters or {}, cursor, limit)
        return c, out

    def row(self, i):
        return (i, uuid.uuid4(), uuid.uuid4(), "x.y", "doc", None, None, 1, "why", "succeeded", uuid.uuid4(), None, T)

    def test_query_is_project_scoped_parameterised_and_newest_first(self):
        u = uuid.uuid4()
        c, _ = self.run_list({"actor_id": u, "action": "x.y", "since": T})
        sql, args = c.execute.call_args.args
        self.assertIn("project_id = %s", sql)
        self.assertIn("ORDER BY id DESC", sql)
        self.assertEqual(args[0], P)
        self.assertIn(u, args)
        self.assertNotIn("x.y", sql)  # values are bound, never interpolated
        self.assertEqual(args[-1], 3)  # limit + 1

    def test_next_cursor_only_when_more_rows_exist(self):
        _, out = self.run_list(rows=[self.row(9), self.row(8), self.row(7)])
        self.assertEqual([i["id"] for i in out["items"]], [9, 8])
        self.assertEqual(a.decode_cursor(out["next_cursor"], P), 8)
        _, out = self.run_list(rows=[self.row(9), self.row(8)])
        self.assertIsNone(out["next_cursor"])

    def test_cursor_adds_a_keyset_condition(self):
        c, _ = self.run_list(cursor=a.encode_cursor(P, 8))
        sql, args = c.execute.call_args.args
        self.assertIn("id < %s", sql)
        self.assertIn(8, args)

    def test_authorization_precedes_the_query(self):
        for exc, code in ((NotFound("p"), 404), (PermissionDenied("participant"), 403)):
            c = MagicMock()
            with patch.object(a, "authorize", side_effect=exc):
                with self.assertRaises(ApiError) as ctx:
                    a._list_events(c, uuid.uuid4(), P, {}, None, 10)
            self.assertEqual(ctx.exception.status_code, code)
            c.execute.assert_not_called()

    def test_credential_redaction_in_reason(self):
        canary = "Failed with Bearer tok-secret-12345 key=sk-live-67890 url=https://u:pass123@example.com/api"
        r = (1, uuid.uuid4(), uuid.uuid4(), "x.y", "doc", None, None, 1, canary, "failed", uuid.uuid4(), None, T)
        _, out = self.run_list(rows=[r])
        reason = out["items"][0]["reason"]
        self.assertNotIn("tok-secret-12345", reason)
        self.assertNotIn("sk-live-67890", reason)
        self.assertNotIn("pass123", reason)
        self.assertIn("[REDACTED]", reason)


class HttpAuditEventsNegativeTests(unittest.TestCase):
    def setUp(self):
        self.auth_patcher = patch.object(a, "authorize", return_value=Decision("owner", None))
        self.auth_patcher.start()
        self.addCleanup(self.auth_patcher.stop)

        app = FastAPI()
        app.include_router(a.router)

        @app.exception_handler(ApiError)
        async def api_error_handler(request, exc: ApiError):
            return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

        self.mock_conn = MagicMock()
        self.mock_conn.execute.return_value.fetchall.return_value = []
        self.actor_id = uuid.uuid4()
        app.dependency_overrides[a.get_db] = lambda: self.mock_conn
        app.dependency_overrides[a.get_authenticated_actor] = lambda: self.actor_id
        self.client = TestClient(app)

    def test_http_limit_bounded_rejection(self):
        res = self.client.get("/api/v1/audit-events", params={"project_id": str(P), "limit": "9" * 5000})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "bad_request")
        self.assertEqual(res.json()["error"]["message"], "limit must be an integer 1..200")

    def test_http_cursor_explicit_empty(self):
        res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor=")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_noncanonical_suffix(self):
        valid_cursor = a.encode_cursor(P, 5)
        for sfx in ("=", "==", "A"):
            with self.subTest(suffix=sfx):
                res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={valid_cursor}{sfx}")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_changed_filters(self):
        c_action = a.encode_cursor(P, 5, filters={"action": "document.imported"})
        res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c_action}&action=benchmark.frozen")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

        res_nofilt = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c_action}")
        self.assertEqual(res_nofilt.status_code, 400)
        self.assertEqual(res_nofilt.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_changed_project(self):
        P2 = uuid.uuid4()
        c_p2 = a.encode_cursor(P2, 5)
        res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c_p2}")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_changed_endpoint(self):
        c_logs = a.encode_cursor(P, 5, endpoint="logs")
        res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c_logs}")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_credential_redaction_in_response(self):
        canary = "Error: Bearer test-bearer-secret key=sk-live-12345 url=https://alice:mypass@service.com/api"
        self.mock_conn.execute.return_value.fetchall.return_value = [
            (1, uuid.uuid4(), self.actor_id, "doc.imported", "doc", None, None, 1, canary, "failed", uuid.uuid4(), None, T)
        ]
        with patch.object(a, "authorize", return_value=Decision("owner", None)):
            res = self.client.get(f"/api/v1/audit-events?project_id={P}")
        self.assertEqual(res.status_code, 200)
        reason = res.json()["items"][0]["reason"]
        self.assertNotIn("test-bearer-secret", reason)
        self.assertNotIn("sk-live-12345", reason)
        self.assertNotIn("mypass", reason)
        self.assertIn("[REDACTED]", reason)

    def test_cursor_never_authority_for_project_or_role(self):
        c = a.encode_cursor(P, 5)
        with patch.object(a, "authorize", side_effect=PermissionDenied("forbidden")):
            res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c}")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "permission_denied")

        with patch.object(a, "authorize", side_effect=NotFound("project not found")):
            res = self.client.get(f"/api/v1/audit-events?project_id={P}&cursor={c}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
