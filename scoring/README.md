# lab_scoring

Rule-based scoring core for #12 (SPEC 9.1, 9.3, 16.5). Only standard-library Python; every intermediate value is an exact `fractions.Fraction`, and floats appear only when a `lab_contracts.score.MetricResult` is built (display rounding `lab-round-1`).

- Geometry: exact union and intersection areas of axis-aligned regions per page, gold-region coverage, and unit hits (every required region ≥ threshold, default 0.80).
- Budget: hits are taken in rank order up to the first hit that would exceed top_k, the token budget or the visual-region budget. That hit and every later hit are excluded, and the result records a `budget_violation`.
- Metrics: Recall@k, Complete@k, Correctness (fact scores 0, 1/2 or 1; 1/2 without a rubric is an `error`, never a number), Textual Consistency (N/A without checks), Localization Precision, Citation Precision (identical citations deduplicated), Citation Completeness and Faithfulness (abstained answers score 0).
- Aggregation: a method failure must be a real scored 0 and stays in the denominator; pending or error results make the aggregate provisional.

Not covered here (#12 integration): mapping submissions to evidence IDs, the semantic judge, caching, the DB/API, and run selection (first valid terminal answer).

Tests: `bash scripts/check.sh --scope scoring`. `tests/scoring/test_cross_checks.py` re-derives the F06 SPEC vectors through the engine and checks the geometry against brute-force grid counting.
