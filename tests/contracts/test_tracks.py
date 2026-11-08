"""raw_pdf_track / shared_parse_track leak guard and comparability (SPEC 19.5, T103)."""
import json
from pathlib import Path
import unittest

from lab_contracts.tracks import (
    FORBIDDEN_FIELDS, PARSE_RESULT_FIELDS, Track, allowed_claims, check_track_input, compare_tracks,
    find_gold_leaks, leaderboard_key,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts/examples/valid"
RAW, SHARED = Track.raw_pdf_track, Track.shared_parse_track
BUNDLE = {"bundle_id": "pb_1", "parser_revision": "parser@3", "bundle_sha256": "a" * 64}


def codes(leaks):
    return sorted((l.path, l.code) for l in leaks)


class LeakTests(unittest.TestCase):
    def test_real_participant_requests_have_no_leaks_in_either_track(self):
        found = False
        for name in ("PrepareRequest", "QueryRequest"):
            for path in EXAMPLES.glob(f"{name}*.json"):
                found = True
                payload = json.loads(path.read_text())
                self.assertEqual(check_track_input(RAW, payload), [], path.name)
                self.assertEqual(check_track_input(SHARED, {**payload, "parse_bundle": BUNDLE}), [], path.name)
        self.assertTrue(found)

    def test_each_forbidden_field_is_found_at_any_depth_and_case(self):
        for name in sorted(FORBIDDEN_FIELDS):
            for payload, path in (({name: 1}, name), ({"config": {"x": [{name.upper(): 1}]}}, f"config.x[0].{name.upper()}")):
                with self.subTest(name=name, path=path):
                    self.assertEqual(codes(find_gold_leaks(payload)), [(path, "forbidden_field")])

    def test_gold_identifiers_and_hashes_are_found_as_values_only(self):
        payload = {"hints": ["e18", "other"], "note": {"ref": "h" * 64}}
        self.assertEqual(codes(find_gold_leaks(payload, gold_identifiers=["e18", "h" * 64])),
                         [("hints[0]", "gold_identifier"), ("note.ref", "gold_identifier")])
        self.assertEqual(find_gold_leaks({"e18": "x"}, gold_identifiers=["e18"]), [])  # a key is not a value
        self.assertEqual(find_gold_leaks(payload, gold_identifiers=["", None, 5]), [])  # ignores junk ids

    def test_leaks_never_copy_the_value(self):
        leaks = find_gold_leaks({"gold_answer": "SECRET-ANSWER"}, gold_identifiers=["SECRET-ID"])
        self.assertNotIn("SECRET", repr(leaks))

    def test_custom_forbidden_set(self):
        self.assertEqual(find_gold_leaks({"gold": 1}, forbidden=frozenset({"other"})), [])
        self.assertEqual(len(find_gold_leaks({"other": 1}, forbidden=frozenset({"other"}))), 1)

    def test_non_gold_names_are_not_flagged(self):
        for name in ("pdf_url", "question", "budget", "document_id", "golden_ratio", "evidence"):
            self.assertEqual(find_gold_leaks({name: 1}), [])


class TrackInputTests(unittest.TestCase):
    def test_raw_track_refuses_any_parser_output(self):
        for name in sorted(PARSE_RESULT_FIELDS):
            with self.subTest(name=name):
                self.assertEqual(codes(check_track_input(RAW, {"config": {name: "x"}})),
                                 [(f"config.{name}", "parse_result_in_raw_track")])

    def test_shared_track_allows_a_declared_parse_but_not_gold(self):
        ok = {"documents": [], "parse_bundle": {**BUNDLE, "parsed_pages": []}}
        self.assertEqual(check_track_input(SHARED, ok), [])
        leaked = {"parse_bundle": BUNDLE, "gold_crops": [1]}
        self.assertEqual(codes(check_track_input(SHARED, leaked)), [("gold_crops", "forbidden_field")])

    def test_shared_track_requires_a_complete_declaration(self):
        self.assertEqual(codes(check_track_input(SHARED, {})), [("parse_bundle", "undeclared_shared_parse")])
        for missing in BUNDLE:
            declared = {k: v for k, v in BUNDLE.items() if k != missing}
            self.assertEqual(codes(check_track_input(SHARED, {"parse_bundle": declared})),
                             [("parse_bundle", "undeclared_shared_parse")])
        self.assertEqual(codes(check_track_input(SHARED, {"parse_bundle": {**BUNDLE, "bundle_id": ""}})),
                         [("parse_bundle", "undeclared_shared_parse")])
        self.assertEqual(codes(check_track_input(SHARED, {"parse_bundle": "pb_1"})),
                         [("parse_bundle", "undeclared_shared_parse")])

    def test_gold_identifiers_apply_to_both_tracks(self):
        self.assertEqual(len(check_track_input(RAW, {"x": "e18"}, gold_identifiers=["e18"])), 1)
        self.assertEqual(len(check_track_input(SHARED, {"parse_bundle": BUNDLE, "x": "e18"}, gold_identifiers=["e18"])), 1)

    def test_invalid_arguments(self):
        with self.assertRaisesRegex(ValueError, "invalid_track"):
            check_track_input("raw_pdf_track", {})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "invalid_payload"):
            check_track_input(RAW, [])  # type: ignore[arg-type]


class ComparabilityTests(unittest.TestCase):
    def test_same_track_benchmark_and_budget_is_comparable(self):
        c = compare_tracks((RAW, "b1", "default"), (RAW, "b1", "default"))
        self.assertEqual((c.comparable, c.blockers, c.limitations), (True, (), ()))

    def test_each_difference_blocks_and_is_named(self):
        self.assertEqual(compare_tracks((RAW, "b1", "default"), (RAW, "b2", "default")).blockers, ("benchmark_revision",))
        self.assertEqual(compare_tracks((RAW, "b1", "default"), (RAW, "b1", "large")).blockers, ("budget_profile",))
        c = compare_tracks((RAW, "b1", "default"), (SHARED, "b1", "default"))
        self.assertEqual((c.comparable, c.blockers), (False, ("track",)))
        self.assertIn("separate_leaderboards", c.limitations)
        c = compare_tracks((RAW, "b1", "default"), (SHARED, "b2", "large"))
        self.assertEqual(c.blockers, ("track", "benchmark_revision", "budget_profile"))

    def test_shared_parse_limitation_is_always_visible(self):
        c = compare_tracks((SHARED, "b1", "default"), (SHARED, "b1", "default"))
        self.assertTrue(c.comparable)  # comparable with each other, still not a parser-quality claim
        self.assertEqual(c.limitations, ("shared_parse_not_independent_parser_quality",))

    def test_leaderboard_key_separates_tracks(self):
        self.assertNotEqual(leaderboard_key(RAW, "b1", "d"), leaderboard_key(SHARED, "b1", "d"))
        self.assertEqual(leaderboard_key(RAW, "b1", "d"), ("raw_pdf_track", "b1", "d"))
        for bad in (("x", "b", "d"), (RAW, "", "d"), (RAW, "b", ""), (RAW, 1, "d")):
            with self.assertRaises(ValueError):
                leaderboard_key(*bad)  # type: ignore[arg-type]

    def test_claims_never_present_a_shared_parse_as_independent_parsing(self):
        self.assertIn("participant_parsing_quality", allowed_claims(RAW))
        self.assertNotIn("participant_parsing_quality", allowed_claims(SHARED))
        self.assertEqual(allowed_claims(SHARED), frozenset({"search_and_answer_on_shared_parse"}))


if __name__ == "__main__":
    unittest.main()
