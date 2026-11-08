"""Public question set endpoint without a database: cursor and limit
validation, the payload allow-list, and the authorization order."""
import unittest
import uuid
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import benchmarks as b
from lab_api.documents import ApiError
from lab_db.repo import Decision, NotFound, PermissionDenied

BID = uuid.uuid4()


class CursorTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(b.decode_cursor(b.encode_cursor(BID, 7), BID), 7)
        self.assertEqual(b.decode_cursor(b.encode_cursor(BID, 0), BID), 0)

    def test_a_cursor_of_another_revision_is_refused(self):
        with self.assertRaises(ApiError) as ctx:
            b.decode_cursor(b.encode_cursor(uuid.uuid4(), 3), BID)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "invalid_cursor"))

    def test_malformed_cursors_are_refused(self):
        import base64, json
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode()
        bad = ["", "!!!", b.encode_cursor(BID, 7) + "!!!!", b.encode_cursor(BID, 7) + "=", "e30", enc([1]), enc({"b": str(BID)}), enc({"b": str(BID), "o": -1}),
               enc({"b": str(BID), "o": True}), enc({"b": str(BID), "o": 2**31}), enc({"b": str(BID), "o": "1"}),
               enc({"b": str(BID), "o": 1, "x": 1}), "A" * 300]
        for c in bad:
            with self.subTest(c=c[:20]):
                with self.assertRaises(ApiError) as ctx:
                    b.decode_cursor(c, BID)
                self.assertEqual(ctx.exception.code, "invalid_cursor")


class LimitTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        self.assertEqual(b.parse_limit(None), b.DEFAULT_LIMIT)
        self.assertEqual(b.parse_limit("1"), 1)
        self.assertEqual(b.parse_limit(str(b.MAX_LIMIT)), b.MAX_LIMIT)
        self.assertEqual(b.parse_limit("0" * 5000 + "1"), 1)
        for bad in ("0", str(b.MAX_LIMIT + 1), "-1", "1.5", "abc", "", " 5", "٣", "9" * 5000):
            with self.subTest(bad=bad):
                with self.assertRaises(ApiError) as ctx:
                    b.parse_limit(bad)
                self.assertEqual(ctx.exception.status_code, 400)


class PublicQuestionTests(unittest.TestCase):
    def row(self, payload):
        return (uuid.uuid4(), "q1", 4, "What?", "a" * 64, "general", payload)

    def test_only_allow_listed_payload_keys_are_copied(self):
        payload = {"language": "en", "tags": ["t"], "answer": "SECRET", "facts": [1], "evidence_sets": [2],
                   "distractors": [3], "gold": {"x": 1}, "visual_object_refs": ["figure_1"], "question_text": "overridden?"}
        item = b.public_question(self.row(payload))
        self.assertEqual(item["language"], "en")
        self.assertEqual(item["question_text"], "What?")  # the column wins
        for leaked in ("answer", "facts", "evidence_sets", "distractors", "gold", "visual_object_refs", "tags", "question_key", "ordinal", "question_hash", "quota_bucket", "modality_requirements", "reasoning_type"):
            self.assertNotIn(leaked, item)

    def test_nested_values_in_public_fields_fail_closed(self):
        for name in b.PUBLIC_PAYLOAD_KEYS:
            with self.subTest(name=name):
                with self.assertRaises(ApiError) as ctx:
                    b.public_question(self.row({name: {"answer": "synthetic-secret"}}))
                self.assertEqual(ctx.exception.status_code, 500)
                self.assertNotIn("synthetic-secret", str(ctx.exception))

    def test_valid_contract_fields_remain_public(self):
        payload = {"language": "en"}
        item = b.public_question(self.row(payload))
        for name, value in payload.items():
            self.assertEqual(item[name], value)

    def test_missing_or_invalid_language_fails_closed(self):
        for p in (None, [], "x", 3, {}, {"language": None}):
            with self.subTest(payload=p):
                with self.assertRaises(ApiError) as ctx:
                    b.public_question(self.row(p))
                self.assertEqual((ctx.exception.status_code, ctx.exception.code),
                                 (500, "invalid_question_payload"))


class AuthorizationOrderTests(unittest.TestCase):
    def call(self, auth_side_effect):
        conn = MagicMock()
        with patch.object(b, "authorize", side_effect=auth_side_effect):
            return conn, b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, None, 10)

    def test_non_member_gets_404_before_any_lookup(self):
        conn = MagicMock()
        with patch.object(b, "authorize", side_effect=NotFound("project")):
            with self.assertRaises(ApiError) as ctx:
                b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, None, 10)
        self.assertEqual(ctx.exception.status_code, 404)
        conn.execute.assert_not_called()

    def test_observer_without_grant_is_403_before_any_lookup(self):
        conn = MagicMock()
        with patch.object(b, "authorize", return_value=Decision("observer", "explicit_grant")):
            with self.assertRaises(ApiError) as ctx:
                b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, None, 10)
        self.assertEqual(ctx.exception.status_code, 403)
        conn.execute.assert_not_called()

    def test_participant_cannot_see_a_draft_and_it_looks_like_a_missing_revision(self):
        def auth(conn, p, a, action):
            if action == "read_gold_and_generation_context":
                raise PermissionDenied("participant")
            return Decision("participant", None)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (BID, "draft")
        with patch.object(b, "authorize", side_effect=auth):
            with self.assertRaises(ApiError) as ctx:
                b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, None, 10)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (404, "not_found"))
        self.assertEqual(conn.execute.call_count, 1)  # no question was read

    def test_empty_cursor_is_not_treated_as_first_page(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (BID, "draft")
        with patch.object(b, "authorize", return_value=Decision("owner", None)):
            with self.assertRaises(ApiError) as ctx:
                b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, "", 10)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "invalid_cursor"))
        self.assertEqual(conn.execute.call_count, 1)

    def test_owner_sees_a_draft_and_paging_reports_the_next_cursor(self):
        conn = MagicMock()
        rows = [(uuid.uuid4(), f"q{i}", i, "T", "b" * 64, "general", {"language": "en"}) for i in range(3)]
        conn.execute.return_value.fetchone.return_value = (BID, "draft")
        conn.execute.return_value.fetchall.return_value = rows
        with patch.object(b, "authorize", return_value=Decision("owner", None)):
            out = b._list_public_questions(conn, uuid.uuid4(), uuid.uuid4(), "bench", 1, None, 2)
        self.assertEqual([i["id"] for i in out["items"]], [str(row[0]) for row in rows[:2]])
        self.assertEqual(b.decode_cursor(out["next_cursor"], BID), 1)
        self.assertEqual(out["status"], "draft")
        # the query asks for limit + 1 rows to know whether there is a next page
        self.assertEqual(conn.execute.call_args_list[-1].args[1], (BID, -1, 3))


if __name__ == "__main__":
    unittest.main()
