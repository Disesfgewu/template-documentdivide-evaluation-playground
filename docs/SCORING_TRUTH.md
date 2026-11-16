# What the scores mean (Issue #197)

**Retrieval gold.** For a question generated since #197 the gold evidence set is the union, over the required facts, of the fewest shown units that
carry each fact (greedy cover of the fact's content words and EVERY number, at least 60 % of its words, at most 4 units per fact). Page numbers,
units inside the top/bottom 8 % of a page, and text that repeats on two or more pages (running headers, footers) are never gold. A fact no such
units carry makes the question invalid (`check:fact_not_attributable`); it is not written. Such a question has `gold_granularity = "fact"`.

A benchmark frozen earlier keeps its gold (frozen versions are immutable): its questions have PAGE-level gold (every unit shown from the page),
and the results say so (`retrieval_gold.page_level_questions` and a note). To get fact-level gold, generate a NEW benchmark revision.

**A source whose quote was not found** (an uploaded developer file) earns no retrieval credit on a fact-level question; it still shows as
`not_found` and counts against citation precision. On a page-level question it keeps the whole-page credit, as before.

**The judge (judge-v3, redesigned after the first real run).** The model first compares each gold fact with the ANSWER only and, for every
`stated` or `contradicted` fact, copies the verbatim `answer_span` that says it (at most 15 words); `missing` has no span. Two worked examples
are in the prompt, and the facts-vs-answer task comes before the claims-vs-passages task. Then code decides, per fact, and records every downgrade:
- **No span, no credit**: a `stated`/`contradicted` verdict whose span is not a (whitespace/case-normalised) substring of the answer becomes
  `missing` (`judge_span_not_in_answer`).
- **A fact without a number**: the span must contain at least 40 % of the fact's content words, else `missing` (`span_does_not_carry_fact`):
  a lenient "stated" for a fact the answer only talks around is not believed.
- **A fact with numbers**, checked on the SPAN: every number must be in it (`number_absent`); at least one of the fact's subjects (named entities
  near the number, either side, not the reference of a comparison such as "than Baseline") must be in it (`entity_absent`); each number must sit in
  a clause (broken at , ; : . ( ) while whereas but) with one of those subjects (`number_other_entity`, the swapped-values case); a direction word
  within 3 tokens that is the opposite of the fact's turns the fact `contradicted`.
Limits: a number written in words is not found; a restated comparison ("B is 19% higher than A" for "A is 19% lower than B") is downgraded; a
paraphrase that shares under 40 % of a number-free fact's words is downgraded. The output cap is 500 tokens (the owner's D7 cap), so the spans are
short and a judged answer may have at most 8 claims. Each downgrade is in the correctness row's reason codes and in the `question_summary` line.
The judge model is chosen per judge run on the page; the estimate uses the configured prices, which belong to the configured model.

**No passage offered** (every quote invented or none given): the prompt says so and the judge still decides each fact from the answer alone;
faithfulness and citation scores stay 0 by construction.
