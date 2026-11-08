"""GET /logs without a database."""
import datetime
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import logs as l
from lab_api.audit import decode_cursor
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


class QueryTests(unittest.TestCase):
    def row(self, i):
        return (i, uuid.uuid4(), T, "INFO", "job.started", "m", "a" * 32, None, None, "api", "c", "op", None, None,
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
        self.assertEqual(decode_cursor(out["next_cursor"], P), 8)
        _, out = self.run_list(rows=[self.row(9)])
        self.assertIsNone(out["next_cursor"])

    def test_authorization_precedes_the_query(self):
        for exc, code in ((NotFound("p"), 404), (PermissionDenied("observer"), 403)):
            c = MagicMock()
            with patch.object(l, "authorize", side_effect=exc):
                with self.assertRaises(ApiError) as ctx:
                    l._list_logs(c, uuid.uuid4(), P, {}, None, 10)
            self.assertEqual(ctx.exception.status_code, code)
            c.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
