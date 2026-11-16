# What the scores mean (Issue #197)

**Retrieval gold.** For a question generated since #197 the gold evidence set is the union, over the required facts, of the fewest shown units that
carry each fact (greedy cover of the fact's content words and EVERY number, at least 60 % of its words, at most 4 units per fact). Page numbers,
units inside the top/bottom 8 % of a page, and text that repeats on two or more pages (running headers, footers) are never gold. A fact no such
units carry makes the question invalid (`check:fact_not_attributable`); it is not written. Such a question has `gold_granularity = "fact"`.

A benchmark frozen earlier keeps its gold (frozen versions are immutable): its questions have PAGE-level gold (every unit shown from the page),
and the results say so (`retrieval_gold.page_level_questions` and a note). To get fact-level gold, generate a NEW benchmark revision.

**A source whose quote was not found** (an uploaded developer file) earns no retrieval credit on a fact-level question; it still shows as
`not_found` and counts against citation precision. On a page-level question it keeps the whole-page credit, as before.

**The judge (judge-v2).** A fact is `stated` only if every number, unit, direction and entity matches; otherwise `contradicted` or `missing`.
Then a code check that the model cannot talk around: every number of the gold fact must be in the answer, bound to the same entity (the nearest
named token to its left, else within 8 tokens) and not with the opposite direction word within 3 tokens. If not, the fact is scored `missing` and the
row carries `judge_overruled_by_number_check` (counted in the results as `questions_overruled_by_number_check`). Limits of the heuristic: it can
downgrade a correct answer that restates a comparison from the other side, or a number written in words; the downgrade is recorded, never silent.

**No passage offered** (every quote invented or none given): the prompt says so and the judge still decides each fact from the answer alone;
faithfulness and citation scores stay 0 by construction.
