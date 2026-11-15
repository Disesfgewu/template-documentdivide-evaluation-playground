# lab_scoring

Rule-based scoring core for #12 (SPEC 9.1, 9.3, 16.5). Only standard-library Python; every intermediate value is an exact `fractions.Fraction`, and floats appear only when a `lab_contracts.score.MetricResult` is built (display rounding `lab-round-1`).

- Geometry: exact union and intersection areas of axis-aligned regions per page, gold-region coverage, and unit hits (every required region ≥ threshold, default 0.80).
- Budget: hits are taken in rank order up to the first hit that would exceed top_k, the token budget or the visual-region budget. That hit and every later hit are excluded, and the result records a `budget_violation`.
- Metrics: Recall@k, Complete@k, Correctness (fact scores 0, 1/2 or 1; 1/2 without a rubric is an `error`, never a number), Textual Consistency (N/A without checks), Localization Precision, Citation Precision (identical citations deduplicated), Citation Completeness and Faithfulness (abstained answers score 0).
- Aggregation: a method failure must be a real scored 0 and stays in the denominator; pending or error results make the aggregate provisional.

- Selection (`selection.py`, T46): each question scores the first valid terminal submission (answered or abstained) in attempt order. Later attempts never replace it. Repairs of transport failures or rejected output are bounded, and when nothing is accepted the question is a method failure that stays in the denominator.
- Comparability (`selection.py`, T45): runs share a ranking only if benchmark revision, question subset hash, grader, budget and observation profile are all equal. A retrieval-only claim also fixes the answerer, and undisclosed answerers are never rankable. Every difference is listed.

- Units (`units.py`, T39/T99): the conversion table is frozen as `lab-units-1`. Units are converted before the tolerance or interval check, with exact values (floats are refused). Percent and percentage points are separate dimensions, and so is each currency (no exchange rates). Temperature and other affine units are unsupported. A wrong, missing, unexpected or unsupported unit fails with its own reason code, even when the number is close.

Not covered here (#12 integration): mapping submissions to evidence IDs, the semantic judge, caching, and the DB/API.

Tests: `bash scripts/check.sh --scope scoring`. `tests/scoring/test_cross_checks.py` re-derives the F06 SPEC vectors through the engine and checks the geometry against brute-force grid counting.
