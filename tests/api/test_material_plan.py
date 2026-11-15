"""The deterministic material plan (P-B, Refs #7): pure function, no database."""
import unittest

from lab_contracts.benchmark import target_counts
from lab_api.material_plan import BUCKETS, HUMAN_REVIEW_NOTE, build_material_plan

LIMIT = "residual limit text"


def plan(pages, promoted, skipped, units):
    return build_material_plan(pages, promoted, skipped, units, LIMIT)


class MaterialPlanTests(unittest.TestCase):
    def test_targets_follow_target_counts(self):
        for pages in (1, 5, 21, 40):
            p = plan(pages, [], {}, {})
            self.assertEqual((p["n_target"], p["q_min"]), target_counts(pages))

    def test_text_only_material_makes_general_cross_page_and_confusable_available_and_chart_table_not(self):
        units = {0: {"text": 2}, 1: {"text": 1}, 2: {"text": 3}}
        p = plan(3, [0, 1, 2], {}, units)
        self.assertEqual({b: p["buckets"][b]["status"] for b in BUCKETS},
                         {"general": "material_available", "cross_page": "material_available",
                          "chart_table": "insufficient_material", "confusable": "material_available"})
        self.assertEqual(p["insufficient_buckets"], ["chart_table"])
        self.assertIn("never guessed", p["buckets"]["chart_table"]["reason"])          # D5
        self.assertIsNone(p["buckets"]["general"]["reason"])
        self.assertEqual((p["pages"]["usable"], p["evidence_units"]), ([0, 1, 2], 6))

    def test_visual_evidence_makes_chart_table_available(self):
        p = plan(2, [0, 1], {}, {0: {"text": 1}, 1: {"table": 1}})
        self.assertEqual(p["buckets"]["chart_table"]["status"], "material_available")
        self.assertEqual(p["buckets"]["chart_table"]["material"], {"table_figure_image_units": 1})

    def test_cross_page_needs_two_clean_text_pages_and_confusable_two_units(self):
        one_page = plan(3, [0], {1: "status_needs_review", 2: "status_needs_review"}, {0: {"text": 1}})
        self.assertEqual(one_page["buckets"]["cross_page"]["status"], "insufficient_material")
        self.assertEqual(one_page["buckets"]["confusable"]["status"], "insufficient_material")   # one unit
        self.assertEqual(one_page["buckets"]["general"]["status"], "material_available")

    def test_skipped_pages_are_reported_with_their_reason_and_a_page_without_units_is_not_usable(self):
        p = plan(4, [0, 1], {2: "unsafe_text:text_not_visible", 3: "status_needs_review"}, {0: {"text": 1}})
        self.assertEqual(p["pages"]["insufficient_material"], {"2": "unsafe_text:text_not_visible", "3": "status_needs_review"})
        self.assertEqual(p["pages"]["usable"], [0])             # page 1 was promoted but carries no evidence unit

    def test_nothing_usable_means_every_bucket_is_insufficient(self):
        p = plan(2, [], {0: "status_needs_review", 1: "status_needs_review"}, {})
        self.assertEqual(p["insufficient_buckets"], list(BUCKETS))
        self.assertEqual(p["evidence_units"], 0)

    def test_human_review_is_skipped_and_compliance_is_partial_never_passed(self):          # D6
        p = plan(1, [0], {}, {0: {"text": 5}})
        self.assertEqual(p["human_review"], {"status": "skipped", "note": HUMAN_REVIEW_NOTE})
        self.assertEqual(p["compliance_projection"], "partial")
        self.assertIn("skipped", HUMAN_REVIEW_NOTE)
        self.assertNotIn("passed", p["human_review"]["status"])

    def test_the_residual_limit_and_the_necessary_condition_caveat_are_always_stated(self):
        p = plan(1, [0], {}, {0: {"text": 5}})
        self.assertIn(LIMIT, p["limits"])
        self.assertTrue(any("necessary conditions only" in x for x in p["limits"]))


if __name__ == "__main__":
    unittest.main()
