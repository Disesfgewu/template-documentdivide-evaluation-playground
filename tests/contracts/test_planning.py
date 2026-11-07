"""Synthetic planner tests; no generation, semantic oracle or provider claim."""
import copy
import itertools
import json
from pathlib import Path
import random
import unittest

from lab_contracts.benchmark import (
    EvidenceUnit, Question, QuotaBucket, SemanticAnswer, SourceAnswer,
    TextualAnswer, ValidationRecord,
)
from lab_contracts.planning import InvalidCandidatePool, plan_candidates

RAW = json.loads((Path(__file__).resolve().parents[2] /
                  "contracts/examples/bundles/minimal.json").read_text())


def pool(specs):
    """Each (hash number, bucket) is one alias with four ID-linked parts."""
    raw = {name: [] for name in ("questions", "semantic_answers", "textual_answers", "source_answers")}
    for ordinal, (number, bucket) in enumerate(specs):
        qid = f"q_{ordinal:04d}"
        for name in raw:
            record = copy.deepcopy(next(r for r in RAW[name] if r["question_id"] == "q_002"))
            record["question_id"] = qid
            if name == "questions":
                record.update(ordinal=ordinal, question_hash=f"{number:064x}", quota_bucket=bucket)
            if name == "source_answers":
                record["validation_refs"] = []
                if bucket == "confusable":
                    record["distractors"] = [{"evidence_id": "e5", "confusion_reason": "synthetic older value",
                                               "disambiguating_condition": "synthetic current year"}]
            raw[name].append(record)
    evidence = copy.deepcopy(RAW["evidence"])
    # Fixed synthetic physical indices all within a twenty-page document.
    for record in evidence:
        record["regions"][0]["page_index"] %= 20
    models = (Question, SemanticAnswer, TextualAnswer, SourceAnswer)
    parts = [[model.model_validate_json(json.dumps(record)) for record in raw[name]]
             for name, model in zip(raw, models)]
    return (*parts, {r["evidence_id"]: EvidenceUnit.model_validate_json(json.dumps(r)) for r in evidence}, {})


def counts(plan):
    return {item.bucket.value: item.selected for item in plan.buckets}


class CandidatePlanTests(unittest.TestCase):
    def test_augmenting_path_recovers_quota_assignment_that_greedy_loses(self):
        # Greedy hash dedup chooses a/b for cross_page and loses both chart
        # positions. Matching must move cross_page to c/d instead.
        specs = [(1, "cross_page"), (1, "chart_table"),
                 (2, "cross_page"), (2, "chart_table"),
                 (3, "cross_page"), (4, "cross_page"),
                 (5, "confusable"), (6, "confusable")]
        specs += [(number, "general") for number in range(7, 21)]
        parts = pool(specs)
        before = copy.deepcopy(parts)
        plan = plan_candidates(20, *parts)
        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.question_deficit, 0)
        self.assertEqual(counts(plan), {"general": 14, "cross_page": 2, "chart_table": 2, "confusable": 2})
        self.assertEqual(len(plan.selected_ids), 20)
        self.assertEqual(len(plan.omitted_ids), 2)
        by_id = {q.question_id: q for q in parts[0]}
        self.assertEqual(len({by_id[qid].question_hash for qid in plan.selected_ids}), 20)
        # IDs select all four parts; neither array order nor tags allocate
        # additional quota positions, and no frozen/source input is edited.
        for records in parts[:4]:
            self.assertEqual(len([r for r in records if r.question_id in plan.selected_ids]), 20)
        self.assertEqual(parts, before)

    def test_result_is_reproducible_under_all_part_and_mapping_order_changes(self):
        specs = [(i, bucket) for i in range(1, 8)
                 for bucket in ("cross_page", "chart_table", "confusable", "general")]
        parts = pool(specs)
        expected = plan_candidates(20, *parts)
        rng = random.Random(41)
        for _ in range(15):
            changed = copy.deepcopy(parts)
            for records in changed[:4]:
                rng.shuffle(records)
            evidence_items = list(changed[4].items())
            rng.shuffle(evidence_items)
            changed = (*changed[:4], dict(evidence_items), changed[5])
            self.assertEqual(plan_candidates(20, *changed), expected)

    def test_deficits_are_visible_and_never_filled_with_fake_records(self):
        parts = pool([(1, "cross_page"), (1, "chart_table"), (2, "confusable")])
        plan = plan_candidates(21, *parts)
        self.assertEqual((plan.n_target, plan.q_min), (21, 3))
        self.assertEqual(plan.status, "needs_review")
        self.assertEqual(len(plan.selected_ids), 2)
        self.assertEqual(plan.question_deficit, 19)
        self.assertEqual(sum(b.deficit for b in plan.buckets), 7)
        self.assertTrue(set(plan.selected_ids) <= {q.question_id for q in parts[0]})

    def test_enough_total_questions_cannot_hide_a_missing_bucket(self):
        plan = plan_candidates(20, *pool([(i, "general") for i in range(30)]))
        self.assertEqual(plan.question_deficit, 0)
        self.assertEqual(len(plan.selected_ids), 20)
        self.assertEqual(plan.status, "needs_review")
        self.assertEqual([b.deficit for b in plan.buckets], [0, 2, 2, 2])

    def test_empty_pool_and_large_target_use_exact_counts(self):
        plan = plan_candidates(600, *pool([]))
        self.assertEqual((plan.n_target, plan.q_min, plan.question_deficit), (600, 60, 600))
        self.assertEqual(plan.status, "needs_review")
        for value in (0, -1, True, "20", 20.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_candidates(value, *pool([]))

    def test_all_candidates_are_validated_even_if_surplus_or_duplicate(self):
        parts = list(pool([(i, "general") for i in range(22)]))
        # A surplus record with an unknown evidence ID cannot be hidden by
        # the N cap. All four candidate parts are validated first.
        source = parts[3][-1].model_dump(mode="json")
        source["evidence_sets"][0]["evidence_ids"] = ["missing"]
        parts[3][-1] = SourceAnswer.model_validate_json(json.dumps(source))
        with self.assertRaises(InvalidCandidatePool) as cm:
            plan_candidates(20, *parts)
        self.assertIn("unknown_evidence_id", {p.code for p in cm.exception.problems})

    def test_missing_part_duplicate_id_and_ordinal_fail_closed(self):
        for kind in ("missing", "duplicate_id", "duplicate_ordinal"):
            parts = list(pool([(1, "general"), (2, "general")]))
            if kind == "missing":
                parts[1].pop()
                expected = "missing_record"
            elif kind == "duplicate_id":
                parts[0].append(parts[0][0])
                expected = "duplicate_question_id"
            else:
                parts[0][1] = parts[0][1].model_copy(update={"ordinal": 0})
                expected = "duplicate_ordinal"
            with self.subTest(kind=kind), self.assertRaises(InvalidCandidatePool) as cm:
                plan_candidates(20, *parts)
            self.assertIn(expected, {p.code for p in cm.exception.problems})

    def test_raw_records_are_not_silently_coerced(self):
        parts = list(pool([(1, "general")]))
        parts[0][0] = parts[0][0].model_dump(mode="json")
        with self.assertRaises(TypeError):
            plan_candidates(20, *parts)
        for index, bad in ((0, iter(parts[0])), (4, []), (5, ())):
            mutated = list(pool([(1, "general")]))
            mutated[index] = bad
            with self.subTest(index=index), self.assertRaises(TypeError):
                plan_candidates(20, *mutated)

    def test_two_hop_augmenting_path_across_all_three_special_buckets(self):
        # First assign hashes 1,2 to cross_page and 3,4 to chart_table.
        # Satisfying confusable on hashes 3,4 must move chart_table -> 1,2
        # and cross_page -> 5,6 in a two-hop augmenting path.
        specs = [
            (1, "cross_page"), (1, "chart_table"),
            (2, "cross_page"), (2, "chart_table"),
            (3, "chart_table"), (3, "confusable"),
            (4, "chart_table"), (4, "confusable"),
            (5, "cross_page"), (6, "cross_page"),
        ]
        specs += [(number, "general") for number in range(7, 21)]
        plan = plan_candidates(20, *pool(specs))
        self.assertEqual(plan.status, "planned")
        self.assertEqual(counts(plan), {"general": 14, "cross_page": 2, "chart_table": 2, "confusable": 2})

    def test_same_hash_and_bucket_chooses_lowest_ordinal_independently_of_order(self):
        specs = [(1, "cross_page"), (1, "cross_page")] + [(number, "general") for number in range(2, 21)]
        parts = pool(specs)
        plan = plan_candidates(20, *parts)
        self.assertIn("q_0000", plan.selected_ids)
        self.assertIn("q_0001", plan.omitted_ids)
        reversed_parts = ([parts[0][1], parts[0][0], *parts[0][2:]], *parts[1:])
        self.assertEqual(plan_candidates(20, *reversed_parts), plan)

    def test_evidence_must_fit_the_physical_document_page_count(self):
        parts = list(pool([(1, "cross_page")]))
        raw = parts[4]["e247"].model_dump(mode="json")
        raw["regions"][0]["page_index"] = 20
        parts[4]["e247"] = EvidenceUnit.model_validate_json(json.dumps(raw))
        with self.assertRaises(InvalidCandidatePool) as cm:
            plan_candidates(20, *parts)
        self.assertIn("page_out_of_document", {p.code for p in cm.exception.problems})

    def test_structural_planning_does_not_turn_validation_into_a_pass(self):
        parts = list(pool([(1, "cross_page")]))
        record = copy.deepcopy(RAW["validations"][1])
        record.update(question_id=parts[0][0].question_id, verdict="needs_review")
        validation = ValidationRecord.model_validate_json(json.dumps(record))
        parts[5][validation.validation_id] = validation
        source = parts[3][0].model_dump(mode="json")
        source["validation_refs"] = [validation.validation_id]
        parts[3][0] = SourceAnswer.model_validate_json(json.dumps(source))
        plan = plan_candidates(20, *parts)
        self.assertEqual(plan.status, "needs_review")
        self.assertEqual(parts[5][validation.validation_id].verdict, "needs_review")
        self.assertFalse(hasattr(plan, "frozen_at"))
        self.assertFalse(hasattr(plan, "compliance"))

    def test_small_random_pools_match_exhaustive_quota_oracle(self):
        rng = random.Random(73)
        special = ("cross_page", "chart_table", "confusable")
        for _ in range(30):
            offers = [tuple(b for b in special if rng.randrange(2)) or ("general",) for _ in range(5)]
            specs = [(i + 1, b) for i, buckets in enumerate(offers) for b in buckets]
            plan = plan_candidates(20, *pool(specs))
            # Independent brute force enumerates one choice per hash.
            best = max(sum(min(2, chosen.count(b)) for b in special)
                       for chosen in itertools.product(*offers))
            got = sum(min(2, b.selected) for b in plan.buckets if b.bucket.value in special)
            self.assertEqual(got, best, offers)


if __name__ == "__main__":
    unittest.main()
