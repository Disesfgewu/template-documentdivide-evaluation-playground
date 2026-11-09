import unittest

from lab_scoring.selection import Attempt, RunProfile, compare_runs, select_answer


def A(n, outcome, sid=None):
    return Attempt(n, outcome, sid)


class SelectAnswerTests(unittest.TestCase):
    def test_t46_first_valid_terminal_is_fixed(self):
        # A later attempt that would score higher never replaces the first accepted answer.
        s = select_answer([A(1, "answered", "s1"), A(2, "answered", "s2_better")], max_repairs=3)
        self.assertEqual((s.submission_id, s.selected_attempt, s.ignored), ("s1", 1, (2,)))

    def test_abstained_is_terminal(self):
        s = select_answer([A(1, "abstained", "s1"), A(2, "answered", "s2")], max_repairs=3)
        self.assertEqual((s.submission_id, s.ignored), ("s1", (2,)))

    def test_transport_and_rejected_output_may_be_repaired(self):
        s = select_answer([A(1, "transport_failure"), A(2, "rejected_output"), A(3, "answered", "s3")], max_repairs=2)
        self.assertEqual((s.submission_id, s.selected_attempt, s.retries_exceeded), ("s3", 3, False))

    def test_repairs_are_bounded(self):
        s = select_answer([A(1, "transport_failure"), A(2, "transport_failure"), A(3, "answered", "s3")], max_repairs=1)
        self.assertEqual((s.submission_id, s.failure_reason, s.ignored, s.retries_exceeded),
                         (None, "transport_failure", (3,), True))

    def test_final_failures_end_the_question(self):
        s = select_answer([A(1, "timeout"), A(2, "answered", "s2")], max_repairs=5)
        self.assertEqual((s.submission_id, s.failure_reason, s.ignored), (None, "timeout", (2,)))
        s = select_answer([A(1, "rejected_output")], max_repairs=0)
        self.assertEqual((s.submission_id, s.failure_reason), (None, "format_error"))
        s = select_answer([], max_repairs=2)
        self.assertEqual((s.submission_id, s.failure_reason), (None, "missing"))

    def test_input_validation(self):
        for bad in ([A(1, "answered", "s"), A(1, "answered", "t")], [A(2, "answered", "s")],
                    [A(1, "answered")], [A(1, "timeout", "s")], [A(1, "best_of_n", "s")]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                select_answer(bad, max_repairs=1)
        for bad in (-1, True, 1.0):
            with self.subTest(max_repairs=bad), self.assertRaises(ValueError):
                select_answer([A(1, "answered", "s")], max_repairs=bad)

    def test_input_order_does_not_matter(self):
        s = select_answer([A(2, "answered", "s2"), A(1, "transport_failure")], max_repairs=1)
        self.assertEqual(s.submission_id, "s2")
        # Listed out of order, attempt 1 is still the first terminal answer.
        s = select_answer([A(2, "answered", "s2"), A(1, "answered", "s1")], max_repairs=1)
        self.assertEqual((s.submission_id, s.ignored), ("s1", (2,)))


def P(run_id, **kw):
    base = dict(benchmark_revision="b1", question_subset_hash="h1", grader_revision="g1",
                budget_profile="default", observation_profile="declared", answerer_model="m1")
    base.update(kw)
    return RunProfile(run_id=run_id, **base)


class CompareRunsTests(unittest.TestCase):
    def test_identical_settings_are_rankable(self):
        c = compare_runs([P("r1"), P("r2")], "full_system")
        self.assertEqual((c.rankable, c.differences), (True, ()))

    def test_t45_every_difference_is_listed(self):
        c = compare_runs([P("r1"), P("r2", benchmark_revision="b2", grader_revision="g2", budget_profile="tight")],
                         "full_system")
        self.assertEqual((c.rankable, c.differences), (False, ("benchmark_revision", "budget_profile", "grader_revision")))
        for key in ("question_subset_hash", "observation_profile"):
            with self.subTest(key=key):
                c = compare_runs([P("r1"), P("r2", **{key: "other"})], "full_system")
                self.assertEqual(c.differences, (key,))

    def test_answerer_rules(self):
        self.assertTrue(compare_runs([P("r1"), P("r2", answerer_model="m2")], "full_system").rankable)
        c = compare_runs([P("r1"), P("r2", answerer_model="m2")], "retrieval_only")
        self.assertEqual(c.differences, ("answerer_model",))
        c = compare_runs([P("r1"), P("r2", answerer_model=None)], "full_system")
        self.assertEqual(c.differences, ("answerer_model_undisclosed",))

    def test_blank_answerer_is_undisclosed(self):
        # PM-REVIEW 6676132303: '' and ' ' disclose nothing, so the runs must not be rankable.
        for claim in ("full_system", "retrieval_only"):
            for models in (("", " "), ("", ""), (" ", "\t"), ("m1", "  ")):
                with self.subTest(claim=claim, models=models):
                    c = compare_runs([P("r1", answerer_model=models[0]),
                                      P("r2", answerer_model=models[1])], claim)
                    self.assertFalse(c.rankable)
                    self.assertIn("answerer_model_undisclosed", c.differences)
        # Different disclosed models stay comparable for a full-system claim.
        c = compare_runs([P("r1", answerer_model="m1"), P("r2", answerer_model="m2")], "full_system")
        self.assertEqual((c.rankable, c.differences), (True, ()))

    def test_bad_input(self):
        for runs, claim in (([P("r1")], "full_system"), ([P("r1"), P("r1")], "full_system"),
                            ([P("r1"), P("r2")], "best")):
            with self.subTest(claim=claim, n=len(runs)), self.assertRaises(ValueError):
                compare_runs(runs, claim)


if __name__ == "__main__":
    unittest.main()
