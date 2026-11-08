"""GET /jobs/{id} without a database."""
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

from lab_api import jobs as j
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

T = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)


def conn(n_transitions=2):
    c = MagicMock()
    head = (uuid.uuid4(), "parse", "stage1", "running", 4, 2, 3, True)
    trans = [("queued", "running", f"r{i}", T) for i in range(n_transitions)]
    c.execute.side_effect = [MagicMock(fetchone=lambda: head), MagicMock(fetchall=lambda: trans)]
    return c


class JobStatusTests(unittest.TestCase):
    def test_fields_and_no_lease_data(self):
        with patch.object(j, "authorize", return_value=Decision("owner", None)) as auth:
            out = j._get_job(conn(), uuid.uuid4(), uuid.uuid4())
        self.assertEqual(auth.call_args.args[3], j.JOB_ACTION)
        self.assertEqual((out["kind"], out["stage"], out["status"], out["attempt"], out["max_attempts"], out["retryable"]),
                         ("parse", "stage1", "running", 2, 3, True))
        self.assertEqual(out["transitions"][0], {"from": "queued", "to": "running", "reason": "r0",
                                                 "occurred_at": T.isoformat()})
        self.assertFalse(out["transitions_truncated"])
        text = repr(out)
        for banned in ("lease", "token", "owner"):
            self.assertNotIn(banned, text)

    def test_transitions_are_capped_and_truncation_reported(self):
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(conn(j.MAX_TRANSITIONS + 1), uuid.uuid4(), uuid.uuid4())
        self.assertEqual(len(out["transitions"]), j.MAX_TRANSITIONS)
        self.assertTrue(out["transitions_truncated"])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(conn(j.MAX_TRANSITIONS), uuid.uuid4(), uuid.uuid4())
        self.assertFalse(out["transitions_truncated"])

    def test_denied_roles_and_hidden_jobs(self):
        c = conn()
        with patch.object(j, "authorize", side_effect=PermissionDenied("participant")):
            with self.assertRaises(ApiError) as ctx:
                j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(c.execute.call_count, 1)  # no transitions read before authorization
        c = conn()
        with patch.object(j, "authorize", side_effect=NotFound("project")):
            with self.assertRaises(ApiError) as ctx:
                j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(c.execute.call_count, 1)
        c = MagicMock()
        c.execute.return_value.fetchone.return_value = None
        with self.assertRaises(ApiError) as ctx:
            j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
