# Question generation control (deterministic decisions)

`lab_contracts.generation_control` holds the decision logic that SPEC 12.2 assigns to two workflow nodes. It is pure: no model call, no clock, no I/O, no persistence. It does not create work or change a question; the workflow executes what it returns and must store `rule_version` (`gen-control-1`) with each decision.

## `classify_candidate(policy, state, outcome, next_estimate_micros)`

Routes one validated candidate to `accept`, `regenerate` or `needs_review`. Amounts are integer micro-units with a currency and pricing revision that must match between policy and state.

The first matching rule wins:

1. **Both source and answer valid.** With `blocking_flags` (for example an unverified visual derivation) the route is `needs_review` with reasons `blocking_flag` plus the flags, sorted. Otherwise `accept`.
2. **Invalid candidate.** An invalid outcome must carry failure codes; without them the input is rejected rather than guessed.
   1. Any code in `NON_REGENERABLE` (`gold_leak`, `policy_violation`, `unsupported_document`, `source_unavailable`): `needs_review`, never regenerated.
   2. `regenerations_used >= max_regenerations`: `needs_review` (`regenerate_limit`).
   3. `unknown_micros > 0`: `needs_review` (`cost_unknown_unreconciled`). A call whose outcome is unknown must be reconciled before another paid call.
   4. `spent + reserved + next_estimate > cost_limit`: `needs_review` (`budget_exhausted`). Equality is allowed; `reserved` includes unknown reservations.
   5. Otherwise `regenerate`; the reasons are the failure codes (to feed the next prompt) and `next_state.regenerations_used` is one higher.

## `check_quotas(policy, deficits, rounds_used)`

Takes the per-bucket deficits (for example from `plan_candidates`) and decides whether to schedule more generation:

- No deficit: `complete`.
- `rounds_used >= max_rounds`: `needs_review` (`quota_round_limit`).
- Otherwise `schedule`: at most `max_new_per_round` new works, granted one at a time to the bucket with the largest remaining deficit (ties by bucket name). `requests` are sorted by bucket and the result does not depend on input order. `deficit_remaining` is reported when the round does not cover everything.

The result only ever adds work; questions that already qualify are not part of the input and are never touched.

## Limits

- It does not judge whether a candidate is correct; `Outcome` comes from the independent validators.
- It does not enforce the limits itself: the executor must feed `next_state` back, add each attempt's settled or reserved cost to the state, and honour the route.
- Real generation, validation and review remain with the provider-backed workflow (#6/#10). This increment does not close #10.

Tests: `tests/contracts/test_generation_control.py` covers every rule, the precedence between rules, the exact budget boundary, loop termination (a candidate that always fails is regenerated at most `max_regenerations` times; a budget for two more attempts allows exactly two), tie-breaks and order independence, and input validation.
