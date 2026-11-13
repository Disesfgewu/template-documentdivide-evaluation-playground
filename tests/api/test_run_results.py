"""GET /runs/{id}/results without a database."""
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

from lab_api import results as r
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

EV, ACTOR, OTHER, RUN, PROJ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)


class QueryParamTests(unittest.TestCase):
    def test_defaults_and_valid(self):
        self.assertEqual(r.parse_query({}), {"limit": 50, "cursor": None})
        q = r.parse_query({"evaluation_id": str(EV), "metric_name": "faithfulness", "quota_bucket": "cross_page", "limit": "7"})
        self.assertEqual((q["evaluation_id"], q["metric_name"], q["quota_bucket"], q["limit"]), (EV, "faithfulness", "cross_page", 7))

    def test_invalid(self):
        for params in ({"evaluation_id": "x"}, {"metric_name": "total"}, {"metric_name": "weighted_total"},
                       {"quota_bucket": "other"}, {"limit": "0"}, {"limit": "201"}, {"limit": "x"}, {"run": "1"},
                       {"limit": "9" * 5000}, {"cursor": ""}):
            with self.subTest(params=params):
                with self.assertRaises(ApiError) as ctx:
                    r.parse_query(params)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_cursor(self):
        self.assertEqual(r.decode_cursor(r.encode_cursor(EV, 3, "correctness"), EV), (3, "correctness"))
        with self.assertRaises(ApiError):
            r.decode_cursor(r.encode_cursor(uuid.uuid4(), 3, "correctness"), EV)
        c_with_filters = r.encode_cursor(EV, 3, "correctness", {"metric_name": "correctness"})
        self.assertEqual(r.decode_cursor(c_with_filters, EV, {"metric_name": "correctness"}), (3, "correctness"))
        with self.assertRaises(ApiError) as ctx:
            r.decode_cursor(c_with_filters, EV, {"metric_name": "faithfulness"})
        self.assertEqual(ctx.exception.code, "invalid_cursor")
        with self.assertRaises(ApiError) as ctx:
            r.decode_cursor(c_with_filters, EV, {})
        self.assertEqual(ctx.exception.code, "invalid_cursor")
        with self.assertRaises(ApiError) as ctx:
            r.decode_cursor(c_with_filters + "!!!!", EV, {"metric_name": "correctness"})
        self.assertEqual(ctx.exception.code, "invalid_cursor")
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode()
        for c in ("", "!!", enc({"e": str(EV), "f": {}, "o": 1, "m": "bogus"}),
                  enc({"e": str(EV), "f": {}, "o": -1, "m": "correctness"}),
                  enc({"e": str(EV), "f": {}, "o": 10**80, "m": "correctness"}),
                  enc({"e": str(EV), "f": {}, "o": True, "m": "correctness"}),
                  enc({"e": str(EV), "f": {}, "o": 1}), "A" * 600):
            with self.subTest(c=c[:10]):
                with self.assertRaises(ApiError) as ctx:
                    r.decode_cursor(c, EV)
                self.assertEqual(ctx.exception.code, "invalid_cursor")


def run_conn(method_owner):
    c = MagicMock()
    c.execute.return_value.fetchone.return_value = (uuid.uuid4(), uuid.uuid4(), method_owner)
    return c


class AuthorizationTests(unittest.TestCase):
    def call(self, decision=None, exc=None, method_owner=OTHER):
        c = run_conn(method_owner)
        with patch.object(r, "authorize", return_value=decision, side_effect=exc):
            try:
                return c, r._authorize_results(c, ACTOR, RUN)
            except ApiError as e:
                return c, e

    def test_gold_readers_pass(self):
        for role in ("owner", "maintainer", "reviewer"):
            self.assertIsInstance(self.call(Decision(role, None))[1], tuple)

    def test_participant_owner_is_403_other_is_404_observer_403(self):
        self.assertEqual(self.call(Decision("participant", "own_run_per_feedback_profile"), method_owner=ACTOR)[1].status_code, 403)
        self.assertEqual(self.call(Decision("participant", "own_run_per_feedback_profile"), method_owner=OTHER)[1].status_code, 404)
        self.assertEqual(self.call(Decision("observer", "published_aggregate"))[1].status_code, 403)

    def test_non_member_404_and_matrix_denial_403(self):
        self.assertEqual(self.call(exc=NotFound("p"))[1].status_code, 404)
        self.assertEqual(self.call(exc=PermissionDenied("x"))[1].status_code, 403)

    def test_unknown_run_404(self):
        c = MagicMock()
        c.execute.return_value.fetchone.return_value = None
        with self.assertRaises(ApiError) as ctx:
            r._authorize_results(c, ACTOR, RUN)
        self.assertEqual(ctx.exception.status_code, 404)


class ResultsShapeTests(unittest.TestCase):
    def conn(self, ev=(EV, "grader@1", "provisional", T), summary=(), rows=()):
        c = MagicMock()
        pid = PROJ
        m_owner = ACTOR
        snapshot = None if ev is None else (ev[0], ev[1], ev[2], ev[3], pid, m_owner, list(summary), list(rows))

        def execute(sql, params=None):
            res = MagicMock()
            if "metric_name = 'correctness'" in sql:                  # the judge summary: nothing was judged here
                res.fetchone.return_value = (0, 0, None, None, None)
            elif "FROM generate_jobs WHERE run_id" in sql:            # does an answering job back this run?
                res.fetchone.return_value = (True,)
            elif "FROM evaluations WHERE run_id" in sql:              # the latest evaluation of the run
                res.fetchone.return_value = (EV,)
            else:
                res.fetchone.return_value = snapshot
                res.fetchall.return_value = []
            return res
        c.execute.side_effect = execute
        return c

    def results(self, c, **q):
        q = {"limit": 2, "cursor": None, **q}
        with patch.object(r, "_authorize_results", return_value=(PROJ, uuid.uuid4())):
            return r._get_results(c, ACTOR, RUN, q)

    def test_metrics_are_reported_separately_with_counts_and_no_total(self):
        out = self.results(self.conn(summary=[("correctness", 2, 0, 1, 0, 50.0), ("faithfulness", 0, 3, 0, 0, None)]))
        self.assertEqual(out["metrics"][0], {"metric_name": "correctness", "scored": 2, "not_applicable": 0, "pending": 1,
                                              "error": 0, "mean": 50.0})
        self.assertTrue(out["provisional"])
        self.assertEqual(out["summary_scope"], "scored_rows_only")
        self.assertIsNone(out["metrics"][1]["mean"])
        self.assertEqual(out["metrics"][1]["not_applicable"], 3)
        self.assertNotIn("total", json.dumps(out).lower().replace("not_applicable", ""))

    def test_items_and_cursor(self):
        row = lambda o, m: (uuid.uuid4(), o, "general", m, "scored", 50.0, 1.0, 2.0, [], None)
        out = self.results(self.conn(rows=[row(0, "correctness"), row(0, "faithfulness"), row(1, "correctness")]))
        self.assertEqual([(i["ordinal"], i["metric_name"]) for i in out["items"]], [(0, "correctness"), (0, "faithfulness")])
        self.assertEqual(r.decode_cursor(out["next_cursor"], EV), (0, "faithfulness"))
        out = self.results(self.conn(rows=[row(0, "correctness")]))
        self.assertIsNone(out["next_cursor"])

    def test_no_evaluation_is_404(self):
        with self.assertRaises(ApiError) as ctx:
            self.results(self.conn(ev=None))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_filters_are_bound_and_cursor_adds_keyset(self):
        c = self.conn()
        self.results(c, metric_name="correctness", quota_bucket="general",
                     cursor=r.encode_cursor(EV, 4, "faithfulness", {"metric_name": "correctness", "quota_bucket": "general"}))
        sql, args = c.execute.call_args.args
        self.assertIn("s.metric_name = %s", sql)
        self.assertIn("qu.quota_bucket = %s", sql)
        self.assertIn("(s.ordinal, s.metric_name) > (%s, %s)", sql)
        self.assertEqual(args[-3:], (4, "faithfulness", 3))

    def test_changed_filter_with_cursor_is_rejected(self):
        c = self.conn()
        cursor = r.encode_cursor(EV, 4, "faithfulness", {"metric_name": "correctness"})
        with self.assertRaises(ApiError) as ctx:
            self.results(c, metric_name="faithfulness", cursor=cursor)
        self.assertEqual(ctx.exception.code, "invalid_cursor")


if __name__ == "__main__":
    unittest.main()
