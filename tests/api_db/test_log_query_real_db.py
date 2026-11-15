"""GET /logs on real PostgreSQL."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.audit import encode_cursor
from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_db.logs import log_event

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

TRACE = "ab" * 16


def day(n):
    return datetime(2026, 1, n, tzinfo=timezone.utc)


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class LogQueryRealDbTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
               "LAB_ARTIFACT_ROOT": str(Path(tmp.name).resolve()),
               "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)

    def log(self, c, project, level, name, d, **ids):
        return log_event(c, level=level, event_name=name, message="m", service="api", component="c",
                         operation_name="op", occurred_at=day(d), project_id=project,
                         attributes={"note": "ATTR-SECRET"}, error_message="ERR-SECRET", **ids)

    def seed(self):
        with self.connect() as c:
            p, owner = self.make_project(c)
            participant = self.add_member(c, p, "participant")
            self.run_id, self.job_id = uuid.uuid4(), uuid.uuid4()
            self.log(c, p, "INFO", "job.started", 2, job_id=self.job_id, trace_id=TRACE)
            self.log(c, p, "WARN", "job.slow", 3, job_id=self.job_id)
            self.log(c, p, "ERROR", "run.failed", 4, run_id=self.run_id, trace_id=TRACE)
            self.log(c, p, "INFO", "run.started", 5, run_id=self.run_id)
            p2, owner2 = self.make_project(c)
            self.log(c, p2, "INFO", "job.started", 2)
            self.log(c, None, "INFO", "system.boot", 2)  # no project: unreachable here
        return p, owner, participant, owner2

    def get(self, actor, project, **params):
        return self.client.get("/api/v1/logs", params={"project_id": str(project), **params},
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_only_this_projects_events_never_attributes_or_error_message(self):
        p, owner, *_ = self.seed()
        res = self.get(owner, p)
        self.assertEqual(res.status_code, 200, res.text)
        items = res.json()["items"]
        self.assertEqual(len(items), 4)
        self.assertNotIn("system.boot", res.text)
        self.assertNotIn("SECRET", res.text)
        self.assertEqual([i["id"] for i in items], sorted((i["id"] for i in items), reverse=True))

    def test_paging_and_filters(self):
        p, owner, *_ = self.seed()
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": "3"}
            if cursor:
                params["cursor"] = cursor
            body = self.get(owner, p, **params).json()
            seen += [i["id"] for i in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(set(seen)), 4)
        n = lambda **kw: len(self.get(owner, p, **kw).json()["items"])
        self.assertEqual(n(level="INFO"), 2)
        self.assertEqual(n(level="ERROR"), 1)
        self.assertEqual(n(event_name="job.started"), 1)
        self.assertEqual(n(trace_id=TRACE), 2)
        self.assertEqual(n(job_id=str(self.job_id)), 2)
        self.assertEqual(n(run_id=str(self.run_id)), 2)
        self.assertEqual(n(since="2026-01-04T00:00:00+00:00"), 2)
        self.assertEqual(n(until="2026-01-04T00:00:00+00:00"), 2)
        self.assertEqual(n(level="INFO", run_id=str(self.run_id)), 1)

    def test_access_and_validation(self):
        p, owner, participant, owner2 = self.seed()
        self.assertEqual(self.get(participant, p).status_code, 403)
        self.assertEqual(self.get(owner2, p).status_code, 404)
        self.assertEqual(self.get(owner, p, level="debug").status_code, 400)
        self.assertEqual(self.get(owner, p, attributes="x").status_code, 400)
        self.assertEqual(self.get(owner, p, cursor="junk").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/logs", params={"project_id": str(p)}).status_code, 401)

    def test_message_credential_redaction_preserves_raw_database(self):
        p, owner, *_ = self.seed()
        raw_message = "Sync failed: Bearer test-bearer-canary-secret key=sk-fake-canary-api-key https://alice:pass-canary-123@db.internal:5432/app"
        evt_id = uuid.uuid4()
        with self.connect() as c:
            with c.transaction():
                c.execute(
                    "INSERT INTO log_events (event_id, schema_version, occurred_at, level, event_name, message, "
                    "service, component, operation_name, project_id, telemetry_origin, redaction_policy) "
                    "VALUES (%s, 1, now(), 'ERROR', 'job.failed', %s, 'api', 'c', 'op', %s, 'lab_observed', 'v1')",
                    (evt_id, raw_message, p),
                )

        # 1. HTTP response must have sensitive credentials redacted
        res = self.get(owner, p, level="ERROR", event_name="job.failed")
        self.assertEqual(res.status_code, 200)
        items = [i for i in res.json()["items"] if i["event_id"] == str(evt_id)]
        self.assertEqual(len(items), 1)
        redacted_msg = items[0]["message"]
        self.assertNotIn("test-bearer-canary-secret", redacted_msg)
        self.assertNotIn("sk-fake-canary-api-key", redacted_msg)
        self.assertNotIn("pass-canary-123", redacted_msg)
        self.assertIn("[REDACTED]", redacted_msg)

        # 2. Append-only raw database log event must retain verbatim raw text unchanged
        with self.connect() as c:
            row = c.execute("SELECT message FROM log_events WHERE event_id = %s", (evt_id,)).fetchone()
            self.assertEqual(row[0], raw_message)

    def insert_raw(self, c, project, evt_id, **over):
        """A row written by a legacy or imported writer: no log_event, no write-time masking or bound."""
        cols = dict(event_name="job.failed", message="m", service="api", component="c", operation_name="op",
                    status=None, error_code=None)
        cols.update(over)
        with c.transaction():
            c.execute(
                "INSERT INTO log_events (event_id, schema_version, occurred_at, level, event_name, message, "
                "service, component, operation_name, status, error_code, project_id, telemetry_origin, "
                "redaction_policy) VALUES (%s, 1, now(), 'ERROR', %s, %s, %s, %s, %s, %s, %s, %s, 'lab_observed', 'v1')",
                (evt_id, cols["event_name"], cols["message"], cols["service"], cols["component"],
                 cols["operation_name"], cols["status"], cols["error_code"], project))

    def test_every_free_text_field_is_redacted_on_read_and_the_raw_row_is_kept(self):
        p, owner, *_ = self.seed()
        ids = {}
        with self.connect() as c:
            for field in ("service", "component", "operation_name", "status", "error_code"):
                ids[field] = uuid.uuid4()
                self.insert_raw(c, p, ids[field], **{field: f"Authorization: Bearer synthetic-{field}-canary"})
        res = self.get(owner, p, level="ERROR", limit="200")
        self.assertEqual(res.status_code, 200)
        for field, evt_id in ids.items():
            self.assertNotIn(f"synthetic-{field}-canary", res.text)
            item = next(i for i in res.json()["items"] if i["event_id"] == str(evt_id))
            self.assertIn("[REDACTED]", item[field])
            self.assertEqual(item["omitted_fields"], [])
        with self.connect() as c:  # the append-only rows keep the verbatim text
            for field, evt_id in ids.items():
                raw = c.execute(f"SELECT {field} FROM log_events WHERE event_id = %s", (evt_id,)).fetchone()[0]
                self.assertEqual(raw, f"Authorization: Bearer synthetic-{field}-canary")

    def test_oversize_values_never_leave_the_database_and_are_omitted_whole(self):
        from lab_api.logs import MAX_MESSAGE_BYTES, OMITTED_FIELD_MARKER, _list_logs

        p, owner, *_ = self.seed()
        evt = uuid.uuid4()
        huge = "z" * 4_194_304
        with self.connect() as c:
            self.insert_raw(c, p, evt, message=huge + " Bearer secret-tail-canary", service="s" * 1_000_000,
                            error_code="e" * 100_000)
            # an exactly-at-the-limit message is returned in full, one byte more is omitted
            at_limit, over = uuid.uuid4(), uuid.uuid4()
            self.insert_raw(c, p, at_limit, event_name="job.atlimit", message="y" * MAX_MESSAGE_BYTES)
            self.insert_raw(c, p, over, event_name="job.overlimit", message="y" * (MAX_MESSAGE_BYTES + 1))
            self.insert_raw(c, p, uuid.uuid4(), event_name="job.multibyte", message="你" * (MAX_MESSAGE_BYTES // 3 + 1))

            class Spy:
                """Records every cell the log query fetched, before any Python projection."""

                def __init__(self, conn):
                    self.conn, self.cells = conn, []

                def __getattr__(self, name):  # transaction(), cursor(), ... pass through untouched
                    return getattr(self.conn, name)

                def execute(self, sql, params=None):
                    cursor = self.conn.execute(sql, params) if params is not None else self.conn.execute(sql)
                    if "FROM log_events" not in str(sql):
                        return cursor
                    spy = self

                    class Recording:
                        def fetchall(self):
                            rows = cursor.fetchall()
                            spy.cells.extend(cell for row in rows for cell in row)
                            return rows

                    return Recording()

            spy = Spy(c)
            out = _list_logs(spy, owner, p, {"level": "ERROR"}, None, 50)
        biggest = max(len(cell) if isinstance(cell, str) else 0 for cell in spy.cells)
        self.assertLessEqual(biggest, max(MAX_MESSAGE_BYTES, 64), "an oversize value was fetched from the database")
        by_name = {i["event_name"]: i for i in out["items"]}
        big = next(i for i in out["items"] if i["event_id"] == str(evt))
        self.assertEqual(sorted(big["omitted_fields"]), ["error_code", "message", "service"])
        self.assertEqual(big["message"], OMITTED_FIELD_MARKER)
        self.assertEqual(by_name["job.atlimit"]["message"], "y" * MAX_MESSAGE_BYTES)
        self.assertEqual(by_name["job.overlimit"]["omitted_fields"], ["message"])
        self.assertEqual(by_name["job.multibyte"]["omitted_fields"], ["message"])

        res = self.get(owner, p, level="ERROR", limit="200")
        self.assertEqual(res.status_code, 200)
        self.assertLess(len(res.content), 64 * 1024)
        self.assertNotIn("secret-tail-canary", res.text)
        with self.connect() as c:  # the raw values are still stored in full
            row = c.execute("SELECT octet_length(message), octet_length(service), octet_length(error_code) "
                            "FROM log_events WHERE event_id = %s", (evt,)).fetchone()
        self.assertEqual(row, (4_194_304 + len(" Bearer secret-tail-canary"), 1_000_000, 100_000))

    def test_cursor_and_limit_regressions_real_db(self):
        p, owner, *_ = self.seed()

        # Bounded invalid decimal input rejected with 400 before int conversion
        r_lim = self.get(owner, p, limit="9" * 5000)
        self.assertEqual(r_lim.status_code, 400)
        self.assertEqual(r_lim.json()["error"]["code"], "bad_request")

        # Explicit empty cursor rejected
        r_empty = self.get(owner, p, cursor="")
        self.assertEqual(r_empty.status_code, 400)
        self.assertEqual(r_empty.json()["error"]["code"], "invalid_cursor")

        # Keyset pagination cursor regressions
        r_page = self.get(owner, p, limit="2")
        self.assertEqual(r_page.status_code, 200)
        cursor = r_page.json()["next_cursor"]
        self.assertIsNotNone(cursor)

        # Noncanonical base64 suffixes rejected
        self.assertEqual(self.get(owner, p, cursor=cursor + "=").status_code, 400)
        self.assertEqual(self.get(owner, p, cursor=cursor + "==").status_code, 400)
        self.assertEqual(self.get(owner, p, cursor=cursor + "A").status_code, 400)

        # Changed filter rejected
        r_changed_filt = self.get(owner, p, cursor=cursor, level="WARN")
        self.assertEqual(r_changed_filt.status_code, 400)
        self.assertEqual(r_changed_filt.json()["error"]["code"], "invalid_cursor")

        # Audit cursor rejected by logs
        audit_cursor = encode_cursor(p, 999, endpoint="audit-events")
        r_audit_cursor = self.get(owner, p, cursor=audit_cursor)
        self.assertEqual(r_audit_cursor.status_code, 400)
        self.assertEqual(r_audit_cursor.json()["error"]["code"], "invalid_cursor")

        # Log cursor rejected by audit-events
        r_logs_at_audit = self.client.get(
            "/api/v1/audit-events",
            params={"project_id": str(p), "cursor": cursor},
            headers={"Authorization": f"Bearer {issue_actor_token(owner, self.secret)}"},
        )
        self.assertEqual(r_logs_at_audit.status_code, 400)
        self.assertEqual(r_logs_at_audit.json()["error"]["code"], "invalid_cursor")


if __name__ == "__main__":
    unittest.main()
