"""GET /comparisons without a database."""
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

from starlette.datastructures import QueryParams

from lab_api import comparisons as c
from lab_api.documents import ApiError

ACTOR = uuid.uuid4()
R1, R2, R3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BENCH, METHOD = uuid.uuid4(), uuid.uuid4()
T = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
METRICS = [{"metric_name": "correctness", "scored": 2, "not_applicable": 1, "pending": 0, "error": 0, "mean": 50.0}]


def params(value):
    return QueryParams([("run_ids", value)])


def snapshot(bench=BENCH, budget="default", grader="g@1", subset="h1", metrics=METRICS, evaluated=True):
    ev = (uuid.uuid4(), grader, "provisional", T) if evaluated else (None, None, None, None)
    return (bench, METHOD, budget, "completed", *ev, metrics if evaluated else [], 3 if evaluated else 0, subset)


class ParseTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(c.parse_run_ids(params(f"{R1},{R2}")), [R1, R2])

    def test_invalid(self):
        for p in (QueryParams(""), params(str(R1)), params(f"{R1},{R1}"), params(f"{R1},x"), params(f"{R1},"),
                  params(",".join(str(uuid.uuid4()) for _ in range(11))),
                  QueryParams([("run_ids", str(R1)), ("run_ids", str(R2))]),
                  QueryParams([("run_ids", f"{R1},{R2}"), ("limit", "1")])):
            with self.subTest(p=str(p)[:40]):
                with self.assertRaises(ApiError) as ctx:
                    c.parse_run_ids(p)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_ten_runs_allowed(self):
        ids = [uuid.uuid4() for _ in range(10)]
        self.assertEqual(c.parse_run_ids(params(",".join(map(str, ids)))), ids)


class CompareTests(unittest.TestCase):
    def compare(self, snapshots, run_ids=None):
        run_ids = run_ids or [R1, R2, R3][:len(snapshots)]
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = snapshots
        with patch.object(c, "_authorize_results", return_value=(uuid.uuid4(), BENCH)) as auth:
            out = c._compare(conn, ACTOR, run_ids)
        return out, auth

    def test_matching_runs_are_comparable_and_metrics_stay_separate(self):
        out, auth = self.compare([snapshot(), snapshot()])
        self.assertTrue(out["comparable"])
        self.assertEqual(out["blockers"], [])
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(out["runs"][0]["metrics"], METRICS)
        self.assertEqual(out["runs"][0]["question_subset"], {"count": 3, "hash": "h1"})
        self.assertTrue(out["provisional"])
        text = json.dumps(out).lower().replace("not_applicable", "")
        for forbidden in ("total", "rank", "winner", "weighted"):
            self.assertNotIn(forbidden, text)

    def test_each_difference_is_named_with_its_values(self):
        for name, other in (("benchmark_revision", snapshot(bench=uuid.uuid4())),
                            ("budget_profile", snapshot(budget="large")),
                            ("grader_revision", snapshot(grader="g@2")),
                            ("question_subset", snapshot(subset="h2"))):
            with self.subTest(name=name):
                out, _ = self.compare([snapshot(), other])
                self.assertFalse(out["comparable"])
                self.assertEqual([b["condition"] for b in out["blockers"]], [name])
                self.assertEqual(set(out["blockers"][0]["values"]), {str(R1), str(R2)})
                self.assertEqual(len(out["runs"]), 2)  # still shown, never ranked

    def test_several_differences_all_listed(self):
        out, _ = self.compare([snapshot(), snapshot(budget="b", grader="g@2")])
        self.assertEqual(sorted(b["condition"] for b in out["blockers"]), ["budget_profile", "grader_revision"])

    def test_run_without_evaluation_blocks_and_is_not_a_zero(self):
        out, _ = self.compare([snapshot(), snapshot(evaluated=False)])
        self.assertFalse(out["comparable"])
        self.assertEqual(out["blockers"], [{"condition": "evaluation", "values": {str(R2): "missing"}}])
        self.assertIsNone(out["runs"][1]["evaluation"])
        self.assertEqual(out["runs"][1]["metrics"], [])

    def test_two_runs_without_evaluation_still_block(self):
        out, _ = self.compare([snapshot(evaluated=False), snapshot(evaluated=False)])
        self.assertFalse(out["comparable"])

    def test_three_runs_one_odd(self):
        out, _ = self.compare([snapshot(), snapshot(), snapshot(grader="g@9")])
        self.assertEqual([b["condition"] for b in out["blockers"]], ["grader_revision"])

    def test_null_mean_stays_null(self):
        na = [{"metric_name": "faithfulness", "scored": 0, "not_applicable": 3, "pending": 0, "error": 0, "mean": None}]
        out, _ = self.compare([snapshot(metrics=na), snapshot(metrics=na)])
        self.assertIsNone(out["runs"][0]["metrics"][0]["mean"])

    def test_unauthorized_run_decides_before_any_data_is_read(self):
        conn = MagicMock()
        calls = []

        def auth(_conn, _actor, run_id):
            calls.append(run_id)
            if run_id == R2:
                raise ApiError(404, "not_found", "run not found")
            return (uuid.uuid4(), BENCH)

        with patch.object(c, "_authorize_results", side_effect=auth):
            with self.assertRaises(ApiError) as ctx:
                c._compare(conn, ACTOR, [R1, R2, R3])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(calls, [R1, R2])  # stops at the first failure
        conn.execute.assert_not_called()

    def test_unknown_run_after_authorization_is_404(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch.object(c, "_authorize_results", return_value=(uuid.uuid4(), BENCH)):
            with self.assertRaises(ApiError) as ctx:
                c._compare(conn, ACTOR, [R1, R2])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_latest_evaluation_and_distinct_questions_are_in_the_sql(self):
        sql = c._SNAPSHOT
        self.assertIn("ORDER BY created_at DESC, id DESC LIMIT 1", sql)
        self.assertIn("DISTINCT question_id", sql)
        self.assertIn("FILTER (WHERE status = 'scored')", sql)


if __name__ == "__main__":
    unittest.main()
