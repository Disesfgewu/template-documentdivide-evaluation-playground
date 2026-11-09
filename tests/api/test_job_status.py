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
                          None, None, None, None, None)]
    else:
        snapshot_rows = []
        for t in trans_rows:
            from_st, to_st, why, at = t[:4]
            if len(t) > 4:
                trunc = t[4]
                out_why = why
            else:
                trunc = len(why) > j.MAX_REASON_CHARS if isinstance(why, str) else False
                out_why = j.REASON_TRUNCATED_MARKER if trunc else why
            snapshot_rows.append(
                (project_id, "parse", "stage1", "running", 4, 2, 3, True,
                 from_st, to_st, out_why, at, trunc)
            )
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
                                                 "reason_truncated": False,
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
        for t in out["transitions"]:
            self.assertFalse(t["reason_truncated"])

        text = repr(out)
        for secret in ("synthetic-secret-key", "secretpass123", "sk-synthetic1234567890abcdef", "synth-session-456"):
            self.assertNotIn(secret, text)

    def test_overlong_reason_fail_closed_marker_and_flag(self):
        overlong = "x" * 2500
        c = conn(transitions=[("queued", "running", overlong, T)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], j.REASON_TRUNCATED_MARKER)
        self.assertTrue(out["transitions"][0]["reason_truncated"])
        self.assertLessEqual(len(out["transitions"][0]["reason"]), j.MAX_REASON_CHARS)

    def test_overlong_large_unicode_fail_closed(self):
        # 2500 characters of multi-byte UTF-8 CJK and emojis (up to 4x bytes)
        unicode_overlong = "測試記錄警報🚨錯誤" * 250
        self.assertGreater(len(unicode_overlong), j.MAX_REASON_CHARS)
        c = conn(transitions=[("queued", "running", unicode_overlong, T)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], j.REASON_TRUNCATED_MARKER)
        self.assertTrue(out["transitions"][0]["reason_truncated"])

    def test_boundary_credential_forms_fail_closed_and_safe(self):
        # Credential patterns placed near/across 2000-char boundary:
        # Bearer/header, PEM, JWToken, key, url
        pk = "PRIVATE" + " KEY"
        pem_key = (
            "-----BEGIN RSA " + pk + "-----\n"
            "MIIEowIBAAKCAQEA0syntheticboundarysecretkeyfakefakefake\n"
            "-----END RSA " + pk + "-----\n"
        )
        jwt_token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        secret_cases = [
            ("bearer", "A" * 1990 + " Authorization: Bearer boundary-secret-bearer-token"),
            ("pem", "B" * 1980 + " " + pem_key),
            ("jwt", "C" * 1985 + " " + jwt_token),
            ("key", "D" * 1990 + " api_key=" + "sk-" + "synthetic9876543210boundarykey"),
            ("url", "E" * 1985 + " https://user:secretpassboundary@llm.example.com/v1"),
        ]
        for name, text_val in secret_cases:
            with self.subTest(case=name):
                self.assertGreater(len(text_val), j.MAX_REASON_CHARS)
                c = conn(transitions=[("queued", "running", text_val, T)])
                with patch.object(j, "authorize", return_value=Decision("owner", None)):
                    out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
                t = out["transitions"][0]
                self.assertEqual(t["reason"], j.REASON_TRUNCATED_MARKER)
                self.assertTrue(t["reason_truncated"])
                out_repr = repr(out)
                for secret in ("boundary-secret-bearer-token",
                               "MIIEowIBAAKCAQEA0syntheticboundarysecretkeyfakefakefake",
                               "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
                               "sk-synthetic9876543210boundarykey",
                               "secretpassboundary"):
                    self.assertNotIn(secret, out_repr)

    def test_exact_boundary_chars(self):
        exact_at_cap = "z" * j.MAX_REASON_CHARS
        c = conn(transitions=[("queued", "running", exact_at_cap, T)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], exact_at_cap)
        self.assertFalse(out["transitions"][0]["reason_truncated"])

        over_cap = "z" * (j.MAX_REASON_CHARS + 1)
        c = conn(transitions=[("queued", "running", over_cap, T)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], j.REASON_TRUNCATED_MARKER)
        self.assertTrue(out["transitions"][0]["reason_truncated"])

    def test_python_defense_in_depth_truncation_when_db_returns_overlong(self):
        # Even if DB returned untruncated string exceeding MAX_REASON_CHARS with truncated=False
        overlong_str = "w" * 2500
        c = conn(transitions=[("queued", "running", overlong_str, T, False)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], j.REASON_TRUNCATED_MARKER)
        self.assertTrue(out["transitions"][0]["reason_truncated"])

    def test_redaction_expansion_is_bounded_after_masking(self):
        reason = "key=x " * 300
        self.assertLessEqual(len(reason), j.MAX_REASON_CHARS)
        self.assertGreater(len(j.redact(reason)), j.MAX_REASON_CHARS)
        c = conn(transitions=[("queued", "running", reason, T)])
        with patch.object(j, "authorize", return_value=Decision("owner", None)):
            out = j._get_job(c, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(out["transitions"][0]["reason"], j.REASON_TRUNCATED_MARKER)
        self.assertTrue(out["transitions"][0]["reason_truncated"])

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
