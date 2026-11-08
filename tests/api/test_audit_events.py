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

    def test_limit(self):
        self.assertEqual(a.parse_limit(None), 50)
        self.assertEqual(a.parse_limit("200"), 200)
        for bad in ("0", "201", "-1", "x", "", "1.0", "٣"):
            with self.subTest(bad=bad):
                with self.assertRaises(ApiError):
                    a.parse_limit(bad)


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


if __name__ == "__main__":
    unittest.main()
