import sys, unittest
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "contracts", _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from lab_contracts.generation_control import *  # noqa  (use explicit names you define in __all__)

import dataclasses
from dataclasses import FrozenInstanceError


class ClassifyCandidateTests(unittest.TestCase):
    """Unit tests for classify_candidate deterministic decision logic (SPEC 12.2)."""

    def setUp(self) -> None:
        self.policy = Policy(
            max_regenerations=2,
            cost_limit_micros=1_000_000,
            currency="USD",
            pricing_revision="2026-v1",
        )
        self.state = AttemptState(
            regenerations_used=0,
            spent_micros=100_000,
            reserved_micros=50_000,
            unknown_micros=0,
            currency="USD",
            pricing_revision="2026-v1",
        )

    def test_accept_when_both_valid_and_no_flags(self) -> None:
        """Rule 1: Candidate with both source and answer valid and no blocking flags routes to accept."""
        outcome = Outcome(source_valid=True, answer_valid=True)
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=100_000)

        self.assertEqual(decision.route, "accept")
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.next_state, self.state)
        self.assertEqual(decision.rule_version, CONTROL_RULE_VERSION)

    def test_both_valid_with_blocking_flags(self) -> None:
        """Rule 1: Candidate with both valid but blocking flags routes to needs_review with sorted reasons."""
        outcome = Outcome(
            source_valid=True,
            answer_valid=True,
            blocking_flags=("unverified_l3", "needs_visual_review"),
        )
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=100_000)

        self.assertEqual(decision.route, "needs_review")
        self.assertEqual(decision.reasons, ("blocking_flag", "needs_visual_review", "unverified_l3"))
        self.assertEqual(decision.next_state, self.state)
        self.assertEqual(decision.rule_version, CONTROL_RULE_VERSION)

    def test_source_invalid_regenerate(self) -> None:
        """Rule 2: Source invalid with regenerations room and budget routes to regenerate with failure codes."""
        outcome = Outcome(
            source_valid=False,
            answer_valid=True,
            failure_codes=("source_low_overlap",),
        )
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=100_000)

        self.assertEqual(decision.route, "regenerate")
        self.assertEqual(decision.reasons, ("source_low_overlap",))
        self.assertEqual(decision.next_state.regenerations_used, 1)
        self.assertEqual(decision.next_state.spent_micros, self.state.spent_micros)
        self.assertEqual(decision.next_state.reserved_micros, self.state.reserved_micros)
        self.assertEqual(decision.next_state.unknown_micros, self.state.unknown_micros)
        self.assertEqual(decision.next_state.currency, self.state.currency)
        self.assertEqual(decision.next_state.pricing_revision, self.state.pricing_revision)

    def test_answer_invalid_regenerate(self) -> None:
        """Rule 2: Answer invalid with regenerations room and budget routes to regenerate."""
        outcome = Outcome(
            source_valid=True,
            answer_valid=False,
            failure_codes=("answer_mismatch",),
        )
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=100_000)

        self.assertEqual(decision.route, "regenerate")
        self.assertEqual(decision.reasons, ("answer_mismatch",))
        self.assertEqual(decision.next_state.regenerations_used, 1)
        self.assertEqual(decision.next_state.spent_micros, self.state.spent_micros)

    def test_both_invalid_sorted_failure_codes(self) -> None:
        """Rule 2: Both invalid candidate returns sorted failure codes in reasons."""
        outcome = Outcome(
            source_valid=False,
            answer_valid=False,
            failure_codes=("answer_mismatch", "source_low_overlap"),
        )
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=100_000)

        self.assertEqual(decision.route, "regenerate")
        self.assertEqual(decision.reasons, ("answer_mismatch", "source_low_overlap"))

    def test_non_regenerable_code_routing(self) -> None:
        """Rule 2a: Non-regenerable code routes to needs_review with non-regenerable codes and not_regenerable."""
        outcome = Outcome(
            source_valid=False,
            answer_valid=False,
            failure_codes=("answer_mismatch", "gold_leak"),
        )
        decision = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=50_000)

        self.assertEqual(decision.route, "needs_review")
        self.assertEqual(decision.reasons, ("gold_leak", "not_regenerable"))
        self.assertEqual(decision.next_state, self.state)

    def test_regenerate_limit_reached(self) -> None:
        """Rule 2b: Reaching max_regenerations limit routes to needs_review with ('regenerate_limit',)."""
        limit_state = dataclasses.replace(self.state, regenerations_used=2)
        outcome = Outcome(
            source_valid=False,
            answer_valid=True,
            failure_codes=("source_low_overlap",),
        )
        decision = classify_candidate(self.policy, limit_state, outcome, next_estimate_micros=50_000)

        self.assertEqual(decision.route, "needs_review")
        self.assertEqual(decision.reasons, ("regenerate_limit",))
        self.assertEqual(decision.next_state, limit_state)

        # Policy with max_regenerations == 0 routes first failure to needs_review
        zero_policy = Policy(max_regenerations=0, cost_limit_micros=1_000_000, currency="USD", pricing_revision="2026-v1")
        dec_zero = classify_candidate(zero_policy, self.state, outcome, next_estimate_micros=50_000)
        self.assertEqual(dec_zero.route, "needs_review")
        self.assertEqual(dec_zero.reasons, ("regenerate_limit",))

    def test_precedence_order(self) -> None:
        """Rule 2 precedence: non_regenerable > regenerate_limit > cost_unknown_unreconciled > budget_exhausted."""
        # Non-regenerable code AND count cap reached -> non-regenerable reasons win
        state_at_cap = dataclasses.replace(self.state, regenerations_used=2)
        outcome_non_regen = Outcome(
            source_valid=False,
            answer_valid=True,
            failure_codes=("gold_leak",),
        )
        dec1 = classify_candidate(self.policy, state_at_cap, outcome_non_regen, next_estimate_micros=50_000)
        self.assertEqual(dec1.route, "needs_review")
        self.assertEqual(dec1.reasons, ("gold_leak", "not_regenerable"))

        # Count cap reached AND unknown cost AND no budget -> regenerate_limit wins
        state_cap_and_unknown = dataclasses.replace(
            self.state,
            regenerations_used=2,
            spent_micros=800_000,
            reserved_micros=200_000,
            unknown_micros=100_000,
        )
        outcome_regen = Outcome(
            source_valid=False,
            answer_valid=True,
            failure_codes=("source_low_overlap",),
        )
        dec2 = classify_candidate(self.policy, state_cap_and_unknown, outcome_regen, next_estimate_micros=100_000)
        self.assertEqual(dec2.route, "needs_review")
        self.assertEqual(dec2.reasons, ("regenerate_limit",))

        # Unknown cost AND no budget (count cap not reached) -> cost_unknown_unreconciled wins
        state_unknown_no_budget = dataclasses.replace(
            self.state,
            regenerations_used=0,
            spent_micros=800_000,
            reserved_micros=200_000,
            unknown_micros=50_000,
        )
        dec3 = classify_candidate(self.policy, state_unknown_no_budget, outcome_regen, next_estimate_micros=100_000)
        self.assertEqual(dec3.route, "needs_review")
        self.assertEqual(dec3.reasons, ("cost_unknown_unreconciled",))

    def test_budget_boundary_and_exhaustion(self) -> None:
        """Rule 2d: Budget limit comparison includes spent, reserved, and next estimate; exact equality allowed."""
        policy = Policy(max_regenerations=5, cost_limit_micros=1_000_000, currency="USD", pricing_revision="v1")
        state = AttemptState(
            regenerations_used=0,
            spent_micros=600_000,
            reserved_micros=100_000,
            unknown_micros=0,
            currency="USD",
            pricing_revision="v1",
        )
        outcome = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))

        # 600_000 + 100_000 + 300_000 = 1_000_000 <= 1_000_000 (allowed)
        dec_exact = classify_candidate(policy, state, outcome, next_estimate_micros=300_000)
        self.assertEqual(dec_exact.route, "regenerate")

        # 600_000 + 100_000 + 300_001 = 1_000_001 > 1_000_000 (exhausted)
        dec_over = classify_candidate(policy, state, outcome, next_estimate_micros=300_001)
        self.assertEqual(dec_over.route, "needs_review")
        self.assertEqual(dec_over.reasons, ("budget_exhausted",))

        # Verify reserved counts towards limit exhaustion
        state_high_reserved = dataclasses.replace(state, spent_micros=100_000, reserved_micros=600_000)
        dec_reserved_over = classify_candidate(policy, state_high_reserved, outcome, next_estimate_micros=300_001)
        self.assertEqual(dec_reserved_over.route, "needs_review")
        self.assertEqual(dec_reserved_over.reasons, ("budget_exhausted",))

    def test_unknown_cost_unreconciled(self) -> None:
        """Rule 2c: Positive unknown_micros requires reconciliation before further attempts even with ample budget."""
        state = AttemptState(
            regenerations_used=0,
            spent_micros=100_000,
            reserved_micros=100_000,
            unknown_micros=50_000,
            currency="USD",
            pricing_revision="2026-v1",
        )
        outcome = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))
        ample_policy = Policy(max_regenerations=5, cost_limit_micros=10_000_000, currency="USD", pricing_revision="2026-v1")

        decision = classify_candidate(ample_policy, state, outcome, next_estimate_micros=50_000)
        self.assertEqual(decision.route, "needs_review")
        self.assertEqual(decision.reasons, ("cost_unknown_unreconciled",))

    def test_validation_errors(self) -> None:
        """Input validation: InvalidControlInput raised for malformed parameters, amounts, or codes."""
        outcome_valid = Outcome(source_valid=True, answer_valid=True)
        outcome_invalid = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))

        # Currency mismatch
        state_diff_currency = dataclasses.replace(self.state, currency="EUR")
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, state_diff_currency, outcome_valid, 50_000)

        # Pricing revision mismatch
        state_diff_rev = dataclasses.replace(self.state, pricing_revision="2027-v1")
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, state_diff_rev, outcome_valid, 50_000)

        # bool amount for next_estimate_micros
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, self.state, outcome_valid, True)  # type: ignore

        # float amount for next_estimate_micros
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, self.state, outcome_valid, 50000.5)  # type: ignore

        # negative amount for next_estimate_micros
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, self.state, outcome_valid, -100)

        # next_estimate_micros == 0
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, self.state, outcome_valid, 0)

        # unknown_micros > reserved_micros in AttemptState
        with self.assertRaises(InvalidControlInput):
            AttemptState(
                regenerations_used=0,
                spent_micros=0,
                reserved_micros=50,
                unknown_micros=100,
                currency="USD",
                pricing_revision="2026-v1",
            )

        # Invalid candidate with empty failure_codes passed to classify_candidate
        outcome_no_codes = Outcome(source_valid=False, answer_valid=True, failure_codes=())
        with self.assertRaises(InvalidControlInput):
            classify_candidate(self.policy, self.state, outcome_no_codes, 50_000)

        # Valid candidate with failure_codes in Outcome
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=True, answer_valid=True, failure_codes=("source_low_overlap",))

        # Bad code format (starting with digit, uppercase, symbols)
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=False, answer_valid=True, failure_codes=("1bad_format",))
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=False, answer_valid=True, failure_codes=("UPPERCASE",))
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=False, answer_valid=True, failure_codes=("hyphen-code",))

        # Unsorted failure codes
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=False, answer_valid=True, failure_codes=("z_code", "a_code"))

        # Duplicate failure codes
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=False, answer_valid=True, failure_codes=("a_code", "a_code"))

        # Policy with max_regenerations -1
        with self.assertRaises(InvalidControlInput):
            Policy(max_regenerations=-1, cost_limit_micros=1_000_000, currency="USD", pricing_revision="2026-v1")

        # Policy with cost_limit_micros 0
        with self.assertRaises(InvalidControlInput):
            Policy(max_regenerations=2, cost_limit_micros=0, currency="USD", pricing_revision="2026-v1")

        # Policy with empty currency
        with self.assertRaises(InvalidControlInput):
            Policy(max_regenerations=2, cost_limit_micros=1_000_000, currency="", pricing_revision="2026-v1")

    def test_determinism_and_immutability(self) -> None:
        """Purity: classify_candidate produces deterministic, frozen Decision objects."""
        outcome = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))
        dec1 = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=50_000)
        dec2 = classify_candidate(self.policy, self.state, outcome, next_estimate_micros=50_000)

        self.assertEqual(dec1, dec2)
        with self.assertRaises(FrozenInstanceError):
            dec1.route = "accept"  # type: ignore

    def test_loop_property_regenerations_cap(self) -> None:
        """Loop property: Candidate failing repeatedly with max_regenerations=3 yields exactly 3 regenerations."""
        policy = Policy(max_regenerations=3, cost_limit_micros=10_000_000, currency="USD", pricing_revision="2026-v1")
        current_state = AttemptState(
            regenerations_used=0,
            spent_micros=0,
            reserved_micros=0,
            unknown_micros=0,
            currency="USD",
            pricing_revision="2026-v1",
        )
        outcome = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))

        routes = []
        for _ in range(5):
            dec = classify_candidate(policy, current_state, outcome, next_estimate_micros=100_000)
            routes.append(dec.route)
            current_state = dec.next_state
            if dec.route != "regenerate":
                break

        self.assertEqual(routes, ["regenerate", "regenerate", "regenerate", "needs_review"])
        self.assertEqual(current_state.regenerations_used, 3)

    def test_loop_property_budget_exhaustion(self) -> None:
        """Loop property: Budget limit allowing exactly 2 regenerations transitions to budget_exhausted on 3rd."""
        # Cost limit: 250_000. Each attempt estimate: 100_000.
        # Initial attempt fails: spent=0 + next 100k <= 250k -> regenerate (attempt 1)
        # Attempt 1 fails: spent=100k + next 100k <= 250k -> regenerate (attempt 2)
        # Attempt 2 fails: spent=200k + next 100k = 300k > 250k -> needs_review ("budget_exhausted")
        policy = Policy(max_regenerations=5, cost_limit_micros=250_000, currency="USD", pricing_revision="2026-v1")
        current_state = AttemptState(
            regenerations_used=0,
            spent_micros=0,
            reserved_micros=0,
            unknown_micros=0,
            currency="USD",
            pricing_revision="2026-v1",
        )
        outcome = Outcome(source_valid=False, answer_valid=True, failure_codes=("source_low_overlap",))

        dec1 = classify_candidate(policy, current_state, outcome, next_estimate_micros=100_000)
        self.assertEqual(dec1.route, "regenerate")
        self.assertEqual(dec1.next_state.regenerations_used, 1)

        # Executor simulates attempt 1 execution, adding estimate to spent
        state_after_att1 = dataclasses.replace(dec1.next_state, spent_micros=100_000)
        dec2 = classify_candidate(policy, state_after_att1, outcome, next_estimate_micros=100_000)
        self.assertEqual(dec2.route, "regenerate")
        self.assertEqual(dec2.next_state.regenerations_used, 2)

        # Executor simulates attempt 2 execution, adding estimate to spent
        state_after_att2 = dataclasses.replace(dec2.next_state, spent_micros=200_000)
        dec3 = classify_candidate(policy, state_after_att2, outcome, next_estimate_micros=100_000)
        self.assertEqual(dec3.route, "needs_review")
        self.assertEqual(dec3.reasons, ("budget_exhausted",))


class CheckQuotasTests(unittest.TestCase):
    """Unit tests for check_quotas deterministic scheduling logic (SPEC 12.2)."""

    def setUp(self) -> None:
        self.policy = QuotaPolicy(max_rounds=5, max_new_per_round=2)

    def test_complete_when_no_deficits(self) -> None:
        """Quota rule 1: Zero or empty deficits return complete status with no work requests."""
        # Empty deficits sequence
        dec_empty = check_quotas(self.policy, [], rounds_used=2)
        self.assertEqual(dec_empty.status, "complete")
        self.assertEqual(dec_empty.requests, ())
        self.assertEqual(dec_empty.reasons, ())
        self.assertEqual(dec_empty.rounds_used_after, 2)
        self.assertEqual(dec_empty.rule_version, CONTROL_RULE_VERSION)

        # All deficits zero
        deficits_zero = [BucketDeficit("a", 0), BucketDeficit("b", 0)]
        dec_zero = check_quotas(self.policy, deficits_zero, rounds_used=2)
        self.assertEqual(dec_zero.status, "complete")
        self.assertEqual(dec_zero.requests, ())
        self.assertEqual(dec_zero.reasons, ())
        self.assertEqual(dec_zero.rounds_used_after, 2)

    def test_largest_deficit_first_rule(self) -> None:
        """Quota rule 3: Deficits {a:3, b:1} with max_new_per_round=2 requests a:2 by largest-first rule."""
        deficits = [BucketDeficit("a", 3), BucketDeficit("b", 1)]
        dec = check_quotas(self.policy, deficits, rounds_used=0)

        self.assertEqual(dec.status, "schedule")
        self.assertEqual(dec.requests, (WorkRequest("a", 2),))
        self.assertEqual(dec.reasons, ("deficit_remaining",))
        self.assertEqual(dec.rounds_used_after, 1)

    def test_tie_breaking_by_bucket_name(self) -> None:
        """Quota rule 3: Equal deficits break ties in bucket name ascending order."""
        policy = QuotaPolicy(max_rounds=5, max_new_per_round=1)
        deficits = [BucketDeficit("b", 2), BucketDeficit("a", 2)]
        dec = check_quotas(policy, deficits, rounds_used=0)

        self.assertEqual(dec.status, "schedule")
        self.assertEqual(dec.requests, (WorkRequest("a", 1),))
        self.assertEqual(dec.reasons, ("deficit_remaining",))

    def test_max_new_larger_than_total_deficit(self) -> None:
        """Quota rule 3: When max_new_per_round exceeds total deficit, all deficits are scheduled with empty reasons."""
        policy = QuotaPolicy(max_rounds=5, max_new_per_round=10)
        deficits = [BucketDeficit("a", 1), BucketDeficit("b", 2)]
        dec = check_quotas(policy, deficits, rounds_used=0)

        self.assertEqual(dec.status, "schedule")
        self.assertEqual(dec.requests, (WorkRequest("a", 1), WorkRequest("b", 2)))
        self.assertEqual(dec.reasons, ())
        self.assertEqual(dec.rounds_used_after, 1)

    def test_quota_round_limit_reached(self) -> None:
        """Quota rule 2: rounds_used >= max_rounds returns needs_review with quota_round_limit."""
        deficits = [BucketDeficit("a", 2)]
        dec = check_quotas(self.policy, deficits, rounds_used=5)

        self.assertEqual(dec.status, "needs_review")
        self.assertEqual(dec.requests, ())
        self.assertEqual(dec.reasons, ("quota_round_limit",))
        self.assertEqual(dec.rounds_used_after, 5)

        # max_rounds == 0 on first call
        zero_policy = QuotaPolicy(max_rounds=0, max_new_per_round=2)
        dec_zero = check_quotas(zero_policy, deficits, rounds_used=0)
        self.assertEqual(dec_zero.status, "needs_review")
        self.assertEqual(dec_zero.reasons, ("quota_round_limit",))
        self.assertEqual(dec_zero.rounds_used_after, 0)

    def test_input_deficit_order_invariance(self) -> None:
        """Quota invariance: Permuting input deficits sequence does not alter QuotaDecision result."""
        d1 = [BucketDeficit("a", 3), BucketDeficit("b", 2), BucketDeficit("c", 1)]
        d2 = [BucketDeficit("c", 1), BucketDeficit("a", 3), BucketDeficit("b", 2)]
        d3 = [BucketDeficit("b", 2), BucketDeficit("c", 1), BucketDeficit("a", 3)]

        res1 = check_quotas(self.policy, d1, rounds_used=0)
        res2 = check_quotas(self.policy, d2, rounds_used=0)
        res3 = check_quotas(self.policy, d3, rounds_used=0)

        self.assertEqual(res1, res2)
        self.assertEqual(res2, res3)

    def test_quota_validation_errors(self) -> None:
        """Quota validation: InvalidControlInput raised for duplicate buckets, empty names, negative deficits, bad rounds, max_new 0."""
        # Duplicate bucket name in deficits
        with self.assertRaises(InvalidControlInput):
            check_quotas(self.policy, [BucketDeficit("a", 1), BucketDeficit("a", 2)], rounds_used=0)

        # Empty bucket name
        with self.assertRaises(InvalidControlInput):
            BucketDeficit("", 1)
        with self.assertRaises(InvalidControlInput):
            BucketDeficit("   ", 1)

        # Negative deficit
        with self.assertRaises(InvalidControlInput):
            BucketDeficit("a", -1)

        # rounds_used negative
        with self.assertRaises(InvalidControlInput):
            check_quotas(self.policy, [BucketDeficit("a", 1)], rounds_used=-1)

        # rounds_used bool
        with self.assertRaises(InvalidControlInput):
            check_quotas(self.policy, [BucketDeficit("a", 1)], rounds_used=True)  # type: ignore

        # max_new_per_round == 0
        with self.assertRaises(InvalidControlInput):
            QuotaPolicy(max_rounds=5, max_new_per_round=0)

    def test_quota_loop_property(self) -> None:
        """Quota loop property: Repeatedly applying quota decisions terminates with round sequence 3,3,3,1 then complete."""
        policy = QuotaPolicy(max_rounds=10, max_new_per_round=3)
        current_deficits = {"a": 5, "b": 5}
        rounds_used = 0
        round_grant_sizes = []

        while True:
            deficits_list = [BucketDeficit(b, d) for b, d in sorted(current_deficits.items())]
            decision = check_quotas(policy, deficits_list, rounds_used)
            if decision.status == "complete":
                break
            self.assertEqual(decision.status, "schedule")
            round_size = sum(r.count for r in decision.requests)
            round_grant_sizes.append(round_size)
            for req in decision.requests:
                current_deficits[req.bucket] -= req.count
            rounds_used = decision.rounds_used_after

        self.assertEqual(round_grant_sizes, [3, 3, 3, 1])
        self.assertEqual(rounds_used, 4)
        self.assertEqual(current_deficits, {"a": 0, "b": 0})


class BlockingFlagValidationTests(unittest.TestCase):
    def test_duplicate_flags_are_rejected_not_silently_merged(self):
        """Flags must be unique; order does not matter because the decision sorts them."""
        with self.assertRaises(InvalidControlInput):
            Outcome(source_valid=True, answer_valid=True, blocking_flags=("a_flag", "a_flag"))
        for flags in (("b_flag", "a_flag"), ("a_flag", "b_flag")):
            o = Outcome(source_valid=True, answer_valid=True, blocking_flags=flags)
            policy = Policy(max_regenerations=1, cost_limit_micros=1000, currency="USD", pricing_revision="p1")
            state = AttemptState(0, 0, 0, 0, "USD", "p1")
            self.assertEqual(classify_candidate(policy, state, o, 1).reasons, ("a_flag", "b_flag", "blocking_flag"))

    def test_flag_format_is_validated(self):
        for bad in ("Bad", "1x", "", "has space", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(InvalidControlInput):
                Outcome(source_valid=True, answer_valid=True, blocking_flags=(bad,))


class QuotaRequestOrderTests(unittest.TestCase):
    def test_requests_are_sorted_by_bucket_whatever_the_input_order(self):
        """The executor may rely on bucket order: the result is order-independent."""
        policy = QuotaPolicy(max_rounds=3, max_new_per_round=3)
        for order in (("c", "a", "b"), ("b", "c", "a"), ("a", "b", "c")):
            d = check_quotas(policy, [BucketDeficit(b, 1) for b in order], 0)
            self.assertEqual([r.bucket for r in d.requests], ["a", "b", "c"], order)
            self.assertEqual(d, check_quotas(policy, [BucketDeficit(b, 1) for b in sorted(order)], 0))


if __name__ == "__main__":
    unittest.main()
