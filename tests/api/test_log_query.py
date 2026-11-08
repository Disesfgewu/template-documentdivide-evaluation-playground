"""GET /logs without a database."""
from __future__ import annotations

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

from lab_api import logs as l
from lab_api.audit import decode_cursor, encode_cursor
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

P = uuid.uuid4()
T = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)


class FilterTests(unittest.TestCase):
    def test_valid(self):
        u, tr = uuid.uuid4(), "a" * 32
        f = l.parse_filters({"project_id": str(P), "level": "WARN", "event_name": "job.started", "trace_id": tr,
                             "run_id": str(u), "question_id": str(u), "job_id": str(u), "operation_id": str(u),
                             "since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00+00:00"})
        self.assertEqual((f["level"], f["trace_id"], f["run_id"]), ("WARN", tr, u))

    def test_invalid(self):
        bad = [{"level": "warn"}, {"level": "TRACE"}, {"event_name": "nodots"}, {"event_name": "A.b"},
               {"event_name": "a." + "b" * 200}, {"trace_id": "A" * 32}, {"trace_id": "a" * 31}, {"trace_id": "a" * 33},
               {"run_id": "x"}, {"job_id": ""}, {"since": "now"}, {"until": "2026-01-01T00:00:00"},
               {"attempt_id": str(uuid.uuid4())}, {"attributes": "x"}]
        for params in bad:
            with self.subTest(params=params):
                with self.assertRaises(ApiError) as ctx:
                    l.parse_filters(params)
                self.assertEqual(ctx.exception.status_code, 400)


class CursorAndLimitTests(unittest.TestCase):
    def test_cursor_round_trip_and_binding(self):
        c = encode_cursor(P, 9, endpoint="logs")
        self.assertEqual(decode_cursor(c, P, endpoint="logs"), 9)
        # Changed project rejected
        with self.assertRaises(ApiError) as ctx:
            decode_cursor(c, uuid.uuid4(), endpoint="logs")
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Audit cursor rejected by logs endpoint
        c_audit = encode_cursor(P, 9, endpoint="audit-events")
        with self.assertRaises(ApiError) as ctx:
            decode_cursor(c_audit, P, endpoint="logs")
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Logs cursor rejected by audit-events endpoint
        with self.assertRaises(ApiError) as ctx:
            decode_cursor(c, P, endpoint="audit-events")
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Malformed payloads rejected
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        for bad_c in ("", "!!", enc([1]), enc({"p": str(P)}), enc({"p": str(P), "i": 0}),
                      enc({"p": str(P), "i": True}), enc({"p": str(P), "i": "3"}),
                      enc({"p": str(P), "i": 3, "x": 1}), "A" * 300):
            with self.subTest(c=bad_c[:12]):
                with self.assertRaises(ApiError) as ctx:
                    decode_cursor(bad_c, P, endpoint="logs")
                self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_cursor_strict_contract_binding_and_rejections(self):
        u = uuid.uuid4()
        f1 = {"level": "INFO", "event_name": "job.started", "trace_id": "a" * 32,
              "run_id": u, "since": T}
        c_f1 = encode_cursor(P, 9, endpoint="logs", filters=f1)

        # Changed filter values rejected
        for bad_filt in (
            {"level": "WARN", "event_name": "job.started", "trace_id": "a" * 32, "run_id": u, "since": T},
            {"level": "INFO", "event_name": "job.ended", "trace_id": "a" * 32, "run_id": u, "since": T},
            {"level": "INFO", "event_name": "job.started", "trace_id": "b" * 32, "run_id": u, "since": T},
            {"level": "INFO", "event_name": "job.started", "trace_id": "a" * 32, "run_id": uuid.uuid4(), "since": T},
            {"level": "INFO", "event_name": "job.started", "trace_id": "a" * 32, "run_id": u, "since": T + datetime.timedelta(days=1)},
            {},
            None,
        ):
            with self.subTest(bad_filt=bad_filt):
                with self.assertRaises(ApiError) as ctx:
                    decode_cursor(c_f1, P, endpoint="logs", filters=bad_filt)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

        c_nofilt = encode_cursor(P, 9, endpoint="logs")
        with self.assertRaises(ApiError) as ctx:
            decode_cursor(c_nofilt, P, endpoint="logs", filters=f1)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Noncanonical base64 suffixes rejected
        for sfx in ("=", "==", "===", "A", " ", "\n"):
            with self.subTest(suffix=sfx):
                with self.assertRaises(ApiError) as ctx:
                    decode_cursor(c_f1 + sfx, P, endpoint="logs", filters=f1)
                self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Explicit empty cursor rejected
        with self.assertRaises(ApiError) as ctx:
            decode_cursor("", P, endpoint="logs")
        self.assertEqual(ctx.exception.code, "invalid_cursor")

        # Malformed JSON and types
        for bad_obj in (
            {"p": str(P), "e": "logs", "f": {}, "o": "id_desc", "i": 0},
            {"p": str(P), "e": "logs", "f": {}, "o": "id_desc", "i": -1},
            {"p": str(P), "e": "logs", "f": {}, "o": "id_desc", "i": "9"},
            {"p": str(P), "e": "logs", "f": {}, "o": "id_desc", "i": True},
            {"p": str(P), "e": "logs", "f": "not-dict", "o": "id_desc", "i": 9},
            {"p": str(P), "e": "logs", "f": {}, "o": "id_asc", "i": 9},
            {"p": str(P), "e": "logs", "f": {}, "o": "id_desc"},
        ):
            raw_b64 = base64.urlsafe_b64encode(json.dumps(bad_obj).encode()).decode().rstrip("=")
            with self.subTest(bad=bad_obj):
                with self.assertRaises(ApiError) as ctx:
                    decode_cursor(raw_b64, P, endpoint="logs")
                self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_limit(self):
        self.assertEqual(l.parse_limit(None), 50)
        self.assertEqual(l.parse_limit("200"), 200)
        for bad_limit in ("0", "201", "-1", "x", "", "1.0", "٣", "9" * 5000, "999999", "000000"):
            with self.subTest(bad=bad_limit):
                with self.assertRaises(ApiError) as ctx:
                    l.parse_limit(bad_limit)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual(ctx.exception.code, "bad_request")


class QueryTests(unittest.TestCase):
    def row(self, i, message="m"):
        return (i, uuid.uuid4(), T, "INFO", "job.started", message, "a" * 32, None, None, "api", "c", "op", None, None,
                None, None, "ok", 1.5, None, "lab_observed")

    def run_list(self, filters=None, cursor=None, limit=2, rows=()):
        c = MagicMock()
        c.execute.return_value.fetchall.return_value = list(rows)
        with patch.object(l, "authorize", return_value=Decision("owner", None)):
            return c, l._list_logs(c, uuid.uuid4(), P, filters or {}, cursor, limit)

    def test_never_selects_attributes_or_error_message(self):
        c, out = self.run_list(rows=[self.row(1)])
        sql = c.execute.call_args.args[0]
        self.assertNotIn("attributes", sql)
        self.assertNotIn("error_message", sql)
        self.assertNotIn("attributes", out["items"][0])
        self.assertNotIn("error_message", out["items"][0])
        self.assertEqual(out["items"][0]["occurred_at"], T.isoformat())

    def test_scoped_to_the_project_bound_and_newest_first(self):
        c, _ = self.run_list({"level": "ERROR", "event_name": "x.y"})
        sql, args = c.execute.call_args.args
        self.assertIn("project_id = %s", sql)
        self.assertIn("ORDER BY id DESC", sql)
        self.assertEqual((args[0], args[-1]), (P, 3))
        self.assertNotIn("x.y", sql)

    def test_paging(self):
        _, out = self.run_list(rows=[self.row(9), self.row(8), self.row(7)])
        self.assertEqual(decode_cursor(out["next_cursor"], P, endpoint="logs"), 8)
        _, out = self.run_list(rows=[self.row(9)])
        self.assertIsNone(out["next_cursor"])

    def test_next_cursor_only_when_more_rows_exist(self):
        _, out = self.run_list(rows=[self.row(9), self.row(8), self.row(7)])
        self.assertEqual([i["id"] for i in out["items"]], [9, 8])
        self.assertEqual(decode_cursor(out["next_cursor"], P, endpoint="logs"), 8)
        _, out = self.run_list(rows=[self.row(9), self.row(8)])
        self.assertIsNone(out["next_cursor"])

    def test_cursor_adds_a_keyset_condition(self):
        c, _ = self.run_list(cursor=encode_cursor(P, 8, endpoint="logs"))
        sql, args = c.execute.call_args.args
        self.assertIn("id < %s", sql)
        self.assertIn(8, args)

    def test_authorization_precedes_the_query(self):
        for exc, code in ((NotFound("p"), 404), (PermissionDenied("observer"), 403)):
            c = MagicMock()
            with patch.object(l, "authorize", side_effect=exc):
                with self.assertRaises(ApiError) as ctx:
                    l._list_logs(c, uuid.uuid4(), P, {}, None, 10)
            self.assertEqual(ctx.exception.status_code, code)
            c.execute.assert_not_called()

    def test_credential_redaction_in_message(self):
        canary = "Failed with Bearer tok-secret-12345 key=sk-live-67890 url=https://u:pass123@example.com/api"
        _, out = self.run_list(rows=[self.row(1, message=canary)])
        msg = out["items"][0]["message"]
        self.assertNotIn("tok-secret-12345", msg)
        self.assertNotIn("sk-live-67890", msg)
        self.assertNotIn("pass123", msg)
        self.assertIn("[REDACTED]", msg)


class HttpLogQueryNegativeTests(unittest.TestCase):
    def setUp(self):
        self.auth_patcher = patch.object(l, "authorize", return_value=Decision("owner", None))
        self.auth_patcher.start()
        self.addCleanup(self.auth_patcher.stop)

        app = FastAPI()
        app.include_router(l.router)

        @app.exception_handler(ApiError)
        async def api_error_handler(request, exc: ApiError):
            return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

        self.mock_conn = MagicMock()
        self.mock_conn.execute.return_value.fetchall.return_value = []
        self.actor_id = uuid.uuid4()
        app.dependency_overrides[l.get_db] = lambda: self.mock_conn
        app.dependency_overrides[l.get_authenticated_actor] = lambda: self.actor_id
        self.client = TestClient(app)

    def test_http_limit_bounded_rejection(self):
        res = self.client.get("/api/v1/logs", params={"project_id": str(P), "limit": "9" * 5000})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "bad_request")
        self.assertEqual(res.json()["error"]["message"], "limit must be an integer 1..200")

    def test_http_cursor_explicit_empty(self):
        res = self.client.get(f"/api/v1/logs?project_id={P}&cursor=")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_noncanonical_suffix(self):
        valid_cursor = encode_cursor(P, 5, endpoint="logs")
        for sfx in ("=", "==", "A"):
            with self.subTest(suffix=sfx):
                res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={valid_cursor}{sfx}")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_audit_cursor_rejected_by_logs(self):
        c_audit = encode_cursor(P, 5, endpoint="audit-events")
        res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c_audit}")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_changed_filters(self):
        c_level = encode_cursor(P, 5, endpoint="logs", filters={"level": "INFO"})
        res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c_level}&level=WARN")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

        res_nofilt = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c_level}")
        self.assertEqual(res_nofilt.status_code, 400)
        self.assertEqual(res_nofilt.json()["error"]["code"], "invalid_cursor")

    def test_http_cursor_changed_project(self):
        P2 = uuid.uuid4()
        c_p2 = encode_cursor(P2, 5, endpoint="logs")
        res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c_p2}")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_cursor")

    def test_http_credential_redaction_in_response(self):
        canary = "Error: Bearer test-bearer-secret key=sk-live-12345 url=https://alice:mypass@service.com/api"
        self.mock_conn.execute.return_value.fetchall.return_value = [
            (1, uuid.uuid4(), T, "ERROR", "job.failed", canary, "a" * 32, None, None, "api", "c", "op", None, None,
             None, None, "err", 1.5, None, "lab_observed")
        ]
        res = self.client.get(f"/api/v1/logs?project_id={P}")
        self.assertEqual(res.status_code, 200)
        msg = res.json()["items"][0]["message"]
        self.assertNotIn("test-bearer-secret", msg)
        self.assertNotIn("sk-live-12345", msg)
        self.assertNotIn("mypass", msg)
        self.assertIn("[REDACTED]", msg)

    def test_cursor_never_authority_for_project_or_role(self):
        c = encode_cursor(P, 5, endpoint="logs")
        with patch.object(l, "authorize", side_effect=PermissionDenied("forbidden")):
            res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c}")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "permission_denied")

        with patch.object(l, "authorize", side_effect=NotFound("project not found")):
            res = self.client.get(f"/api/v1/logs?project_id={P}&cursor={c}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
