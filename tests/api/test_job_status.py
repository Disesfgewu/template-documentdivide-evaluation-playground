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


def conn(n_transitions=2, project_id=None, transitions=None):
    if project_id is None:
        project_id = uuid.uuid4()
    c = MagicMock()
    # Query 1: Initial authorization gate project_id lookup
    auth_row = (project_id,)
    # Query 2: Snapshot query returning (job_fields..., transition_fields...)
    if transitions is None:
        trans_rows = [("queued", "running", f"r{i}", T) for i in range(n_transitions)]
    else:
        trans_rows = transitions

    if not trans_rows:
        snapshot_rows = [(project_id, "parse", "stage1", "running", 4, 2, 3, True,
                          None, None, None, None)]
    else:
        snapshot_rows = [
            (project_id, "parse", "stage1", "running", 4, 2, 3, True,
             t[0], t[1], t[2], t[3])
            for t in trans_rows
        ]
    c.execute.side_effect = [MagicMock(fetchone=lambda: auth_row), MagicMock(fetchall=lambda: snapshot_rows)]
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
        self.assertEqual(c.execute.call_count, 1)  # only initial project_id lookup before authorization
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
        self.assertEqual(c.execute.call_count, 1)

    def test_secret_patterns_in_transitions_are_redacted(self):
        secret_transitions = [
            ("queued", "running", "Authorization: Bearer synthetic-secret-key", T),
            ("running", "failed", "error at https://user:secretpass123@llm.example.com/v1", T),
            ("queued", "running", "api_key=sk-synthetic1234567890abcdef", T),
            ("running", "failed", "Cookie: session=synth-session-456", T),
            ("queued", "running", "normal worker started processing", T),
        ]
        c = conn(transitions=secret_transitions)
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())

        reasons = [t["reason"] for t in out["transitions"]]
        self.assertEqual(reasons[0], "Authorization: [REDACTED]")
        self.assertEqual(reasons[1], "error at https://user:[REDACTED]@llm.example.com/v1")
        self.assertEqual(reasons[2], "api_key=[REDACTED]")
        self.assertEqual(reasons[3], "Cookie: [REDACTED]")
        self.assertEqual(reasons[4], "normal worker started processing")

        text = repr(out)
        for secret in ("synthetic-secret-key", "secretpass123", "sk-synthetic1234567890abcdef", "synth-session-456"):
            self.assertNotIn(secret, text)

    def test_snapshot_single_query_structure(self):
        c = conn(0)
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(c.execute.call_count, 2)
        # First call is authorization project_id lookup
        first_query = c.execute.call_args_list[0].args[0]
        self.assertIn("SELECT project_id FROM jobs WHERE id = %s", first_query)
        # Second call is the single-statement snapshot query
        second_query = c.execute.call_args_list[1].args[0]
        self.assertIn("FROM jobs j", second_query)
        self.assertIn("LEFT JOIN", second_query)
        self.assertIn("job_transitions", second_query)
        self.assertEqual(out["transitions"], [])
        self.assertFalse(out["transitions_truncated"])


if __name__ == "__main__":
    unittest.main()
