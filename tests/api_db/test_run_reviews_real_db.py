"""A reviewed correctness per question next to the judge's value: append-only, latest wins, judge rows untouched, gold-side only (Issue #218)."""
from __future__ import annotations

import unittest
import uuid

from tests.api_db import test_external_submission_real_db as upload_tests
from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

import psycopg


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RunReviewsRealDbTests(GenerateApiFixtures, FreshDatabase):
    """Uses the fixtures of the developer-upload tests (a frozen benchmark and an uploaded answers file) without inheriting, and so re-running, their tests."""
    worker, frozen, get, gold, file, upload = (upload_tests.ExternalSubmissionRealDbTests.__dict__[n] for n in ("worker", "frozen", "get", "gold", "file", "upload"))

    def run_with_judge_scores(self):
        """An uploaded run whose first three questions carry a SCORED judge correctness of 33.33, 100 and 0 (set directly: the judge itself is tested elsewhere)."""
        project, owner = self.frozen()
        run = self.upload(owner, project, self.file(owner, project)).json()["run_id"]
        keys = [i["question_id"] for i in self.get(owner, project, f"/api/v1/runs/{run}/submission-details").json()["items"]]
        with self.connect() as c:
            ev = c.execute("SELECT id FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (run,)).fetchone()[0]
            for key, (num, den) in zip(keys[:3], ((1, 3), (3, 3), (0, 3))):
                updated = c.execute(
                    "UPDATE question_scores SET status = 'scored', raw_value = %s, numerator = %s, denominator = %s, reason_codes = '{}' "
                    "WHERE evaluation_id = %s AND metric_name = 'correctness' AND question_id = (SELECT id FROM active_questions WHERE question_key = %s "
                    "AND benchmark_id = question_scores.benchmark_id)", (100.0 * num / den, num, den, ev, key)).rowcount
                self.assertEqual(updated, 1)
            c.commit()
        return project, owner, run, keys

    def review(self, who, project, run, reviews):
        return self.client.post(f"/api/v1/runs/{run}/reviews?project_id={project}", headers=self.headers(who), json={"reviews": reviews})

    @staticmethod
    def one(key, value, kind="model_assisted", label="PM (Claude)", note=None):
        return {"question_id": key, "value": value, "reviewer_kind": kind, "reviewer_label": label, "note": note}

    def test_a_review_sits_next_to_the_judge_value_and_never_changes_it(self):
        project, owner, run, keys = self.run_with_judge_scores()
        res = self.review(owner, project, run, [self.one(keys[0], 100, note="asks only for the threshold"), self.one(keys[3], 0)])
        self.assertEqual(res.status_code, 201, res.text)
        summary = res.json()["summary"]
        self.assertEqual((summary["questions_reviewed"], summary["reviewed_by"], summary["judge_scored"]), (2, {"human": 0, "model_assisted": 2}, 3))
        self.assertAlmostEqual(summary["judge_mean"], (100 / 3 + 100 + 0) / 3, places=3)
        # reviewed where there is a review (keys[0] -> 100, keys[3] -> 0, which the judge never scored), the judge's value otherwise
        self.assertEqual(summary["questions_in_reviewed_mean"], 4)
        self.assertAlmostEqual(summary["reviewed_mean"], (100 + 100 + 0 + 0) / 4, places=3)
        with self.connect() as c:
            judge = c.execute("SELECT raw_value FROM question_scores qs JOIN active_questions q ON q.id = qs.question_id WHERE qs.run_id = %s "
                              "AND qs.metric_name = 'correctness' AND q.question_key = %s", (run, keys[0])).fetchone()[0]
            self.assertAlmostEqual(judge, 100 / 3, places=6)               # the judge's own row is as it was
            self.assertEqual(c.execute("SELECT count(*) FROM audit_events WHERE action = 'run.score_reviewed' AND resource_id = %s", (run,)).fetchone()[0], 1)
        listed = self.get(owner, project, f"/api/v1/runs/{run}/reviews").json()
        first = next(i for i in listed["items"] if i["question_id"] == keys[0])
        self.assertEqual((first["value"], first["reviewer_kind"], first["reviewer_label"], first["note"], first["versions"]),
                         (100.0, "model_assisted", "PM (Claude)", "asks only for the threshold", 1))
        self.assertTrue(listed["contains_gold"])

    def test_the_latest_review_wins_and_every_earlier_one_stays(self):
        project, owner, run, keys = self.run_with_judge_scores()
        self.assertEqual(self.review(owner, project, run, [self.one(keys[0], 100)]).status_code, 201)
        self.assertEqual(self.review(owner, project, run, [self.one(keys[0], 40, kind="human", label="owner")]).status_code, 201)
        listed = self.get(owner, project, f"/api/v1/runs/{run}/reviews").json()
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual((listed["items"][0]["value"], listed["items"][0]["reviewer_kind"], listed["items"][0]["versions"]), (40.0, "human", 2))
        self.assertEqual(listed["summary"]["reviewed_by"], {"human": 1, "model_assisted": 0})
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM run_score_reviews WHERE run_id = %s", (run,)).fetchone()[0], 2)
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                c.execute("UPDATE run_score_reviews SET value = 0 WHERE run_id = %s", (run,))
            c.rollback()
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                c.execute("DELETE FROM run_score_reviews WHERE run_id = %s", (run,))

    def test_a_bad_batch_records_nothing(self):
        project, owner, run, keys = self.run_with_judge_scores()
        good = self.one(keys[0], 100)
        for body, status in (([good, self.one("q-nope", 10)], 422), ([good, good], 422), ([self.one(keys[1], 101)], 400), ([self.one(keys[1], -1)], 400),
                             ([{**good, "reviewer_kind": "robot"}], 400), ([{**good, "extra": 1}], 400), ([self.one(keys[1], 10, label="  ")], 400),
                             ([self.one(keys[1], 10, note="x" * 501)], 400), ([], 400), ([self.one(f"k{i}", 1) for i in range(101)], 400)):
            with self.subTest(status=status, size=len(body)):
                self.assertEqual(self.review(owner, project, run, body).status_code, status)
        self.assertEqual(self.client.post(f"/api/v1/runs/{run}/reviews?project_id={project}", headers=self.headers(owner), json={"reviews": [good], "x": 1}).status_code, 400)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM run_score_reviews").fetchone()[0], 0)

    def test_only_the_gold_side_can_read_or_write_reviews_and_no_developer_endpoint_returns_them(self):
        project, owner, run, keys = self.run_with_judge_scores()
        self.assertEqual(self.review(owner, project, run, [self.one(keys[0], 100)]).status_code, 201)
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            reviewer = self.add_member(c, project, "reviewer")
            participant = self.add_member(c, project, "participant")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        for who, expect in ((participant, 403), (reviewer, 403), (outsider, 404)):
            self.assertEqual(self.get(who, project, f"/api/v1/runs/{run}/reviews").status_code, expect)
            self.assertEqual(self.review(who, project, run, [self.one(keys[1], 5)]).status_code, expect)
        self.assertEqual(self.get(maintainer, project, f"/api/v1/runs/{run}/reviews").status_code, 200)
        self.assertEqual(self.review(maintainer, project, run, [self.one(keys[1], 5, kind="human", label="maintainer")]).status_code, 201)
        results = self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner))
        self.assertNotIn("review", results.text.lower())                    # the results the developer side reads do not carry reviewed values
        self.assertEqual(self.client.get(f"/api/v1/runs/{uuid.uuid4()}/reviews?project_id={project}", headers=self.headers(owner)).status_code, 404)

