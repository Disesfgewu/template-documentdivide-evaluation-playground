"""GET /runs/{id} without a database: who may see a run, and what is counted."""
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

from lab_api import runs as r
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

ACTOR, OWNER_OF_METHOD, RUN = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def conn(method_owner, total=4, attempts=5, by_status=(("accepted", 3), ("rejected", 1), ("late", 1)),
         by_answer=(("answered", 2), ("abstained", 1)), finalized=None):
    c = MagicMock()
    head = (uuid.uuid4(), "evaluating", "default", finalized, uuid.uuid4(), uuid.uuid4(), method_owner, "bench", 2)
    results = [MagicMock(fetchone=lambda: head), MagicMock(fetchone=lambda: (total,)),
               MagicMock(fetchone=lambda: (attempts,)), MagicMock(fetchall=lambda: list(by_status)),
               MagicMock(fetchall=lambda: list(by_answer))]
    c.execute.side_effect = results
    return c


class RunStatusTests(unittest.TestCase):
    def get(self, c, decision):
        with patch.object(r, "authorize", return_value=decision):
            return r._get_run(c, ACTOR, RUN)

    def test_gold_readers_see_any_run_with_progress_counts(self):
        for role in ("owner", "maintainer", "reviewer"):
            with self.subTest(role=role):
                out = self.get(conn(OWNER_OF_METHOD), Decision(role, None))
                self.assertEqual(out["progress"], {"question_count": 4, "attempts": 5, "accepted_answered": 2,
                                                   "accepted_abstained": 1, "pending": 1, "rejected_submissions": 1,
                                                   "late_submissions": 1})
                self.assertEqual((out["status"], out["benchmark"]["revision"], out["finalized_at"]), ("evaluating", 2, None))

    def test_pending_never_goes_negative(self):
        out = self.get(conn(OWNER_OF_METHOD, total=1), Decision("owner", None))
        self.assertEqual(out["progress"]["pending"], 0)

    def test_finalized_at_is_iso(self):
        t = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.get(conn(OWNER_OF_METHOD, finalized=t), Decision("owner", None))["finalized_at"], t.isoformat())

    def test_participant_sees_only_their_own_methods_run(self):
        self.assertEqual(self.get(conn(ACTOR), Decision("participant", None))["status"], "evaluating")
        with self.assertRaises(ApiError) as ctx:
            self.get(conn(OWNER_OF_METHOD), Decision("participant", None))
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (404, "not_found"))

    def test_observer_gets_403(self):
        with self.assertRaises(ApiError) as ctx:
            self.get(conn(OWNER_OF_METHOD), Decision("observer", "published_aggregate"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_non_member_and_unknown_run_are_404_with_no_counts_read(self):
        c = conn(OWNER_OF_METHOD)
        with patch.object(r, "authorize", side_effect=NotFound("project")):
            with self.assertRaises(ApiError) as ctx:
                r._get_run(c, ACTOR, RUN)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(c.execute.call_count, 1)
        c2 = MagicMock()
        c2.execute.return_value.fetchone.return_value = None
        with self.assertRaises(ApiError) as ctx:
            r._get_run(c2, ACTOR, RUN)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_permission_denied_from_the_matrix_is_403(self):
        with patch.object(r, "authorize", side_effect=PermissionDenied("x")):
            with self.assertRaises(ApiError) as ctx:
                r._get_run(conn(OWNER_OF_METHOD), ACTOR, RUN)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
