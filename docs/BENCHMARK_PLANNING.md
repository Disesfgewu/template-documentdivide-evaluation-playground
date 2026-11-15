# Draft benchmark candidate planning

`lab_contracts.planning.plan_candidates` implements the deterministic selection
step in SPEC 5.1. It consumes existing `Question`, `SemanticAnswer`,
`TextualAnswer`, `SourceAnswer`, `EvidenceUnit` and `ValidationRecord` objects.
Use the existing strict contracts to parse untrusted JSON first. The result
contains selected question IDs, omitted IDs, one count/deficit per bucket,
the total question deficit and `selection_rule_version`.

```python
from lab_contracts.planning import plan_candidates

plan = plan_candidates(
    page_count, questions, semantic_answers, textual_answers, source_answers,
    evidence_by_id, validation_by_id,
)
selected_questions = [q for q in questions if q.question_id in plan.selected_ids]
```

Callers join **all four parts by the selected IDs**, never by array position.
They must retain the selection rule version with subsequent operations.
The function changes no candidate, answer, evidence or validation record.
It creates no revision, job, cache entry or public projection.

The planner validates the entire pool, including surplus candidates, using
the existing cross-part validator and physical page bounds. Located failures
raise `InvalidCandidatePool` with `.problems`. Exact repeated hashes are
permitted in the candidate pool but only one alias may be selected. Distinct
question IDs and ordinals remain required. Caller-supplied hashes do not
prove semantic uniqueness; semantic deduplication remains a verifier gate.

For `P` physical pages, the target is `N=max(20,P)` and each special bucket
requires `Q=ceil(N/10)` distinct questions. A candidate has exactly one
declared bucket; tags allocate no extra positions. The selection first
matches the three special-bucket requirements against distinct hashes. If
an earlier alias blocks a later bucket, an augmenting path can move it to a
different eligible alias. This avoids the false deficits caused by greedy
deduplication. Remaining distinct hashes fill the target. Input ordering
does not affect selection: hash, ordinal and question ID provide stable
ties. Selected/omitted IDs are returned in ordinal/ID order.

`needs_review` exposes a short total or any short special bucket. No question
or source is invented to fill a gap. `planned` means only that the structural
draft selection meets counts. **Neither status authorizes freeze or claims
a compliant benchmark.** The planner does not turn `fail`/`needs_review`
validation records into passes or approve their content. Independent
semantic verification, source necessity/ablation, visual reasoning diversity,
numeric/units checks, human gates and publishing remain required by SPEC
5.3/19.5. Partial benchmark exceptions must be explicit in the later
publication workflow; this primitive cannot silently publish a partial.

## Verification and remaining integration

Run `scripts/check.sh --scope 'contracts fixtures governance'` with the locked
development environment. `tests/contracts/test_planning.py` covers the
greedy-fails assignment, deterministic permutations, duplicate hashes,
unknown/missing IDs, physical bounds, total/bucket deficits, unchanged inputs
and a separate exhaustive oracle for small random pools. Fixtures are
synthetic contract data, not real parser or provider evidence.

This increment covers R04/R05 deterministic T14–T16 subcases. It does not
complete T16 integration/quality, T18–T24 generation/recovery or any release
acceptance condition. #9 supplies verified evidence and relations; #5/#6
provide durable revisions/jobs/cache. LLM generation, independent verifier
and human approval, draft/freeze API orchestration and full #10 integration
remain open. Existing four-part schemas and freeze semantics are unchanged.
