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
`stated` or `contradicted` fact, copies the verbatim `answer_span` that says it (at most 30 words); `missing` has no span. Two worked examples
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
Three refinements after the second real run: (1) a NUMBER is free-standing: digits inside identifiers (all-0s, all-1s, Ge2Sb2Te5, CPU2017, 3D) are
not numbers, and a hyphenated word is an entity name only when it has a capital (Flip-N-Write, SET-bit; not all-1s); (2) the numbers and entities are
read in the whole SENTENCE of the answer that holds the span (the span itself must still be verbatim), so a condition stated before the copied
piece counts; (3) the judge receives the QUESTION and reads the answer as a reply to it ("60%." answers "what fraction ...?"): the question's words
count toward a fact's entity and coverage, but the span must itself bring at least one of the fact's words that the question lacks (the value or name).

**Judge model and its price.** The model is chosen per judge run on the page. Its price is looked up by model name: `LAB_GEN_MODEL_PRICES` (owner JSON,
`{"claude-sonnet-5-5": [2000000, 10000000]}`, micro-USD per million tokens, in and out) > a built-in table > the configured `LAB_GEN_PRICE_*` pair (the owner's
model, claude-haiku-4-5 by default, which is deliberately not in the table). Built-in, UNVERIFIED list prices (the Claude API reference table cached
2026-11-03; check them against your invoice): haiku-5-5 $0.10/$0.50 (prompts up to 100K tokens), sonnet-5-5 $2/$10, opus-5-5 $4/$20 per million tokens. The estimate,
the derived cap and the worker's charge all use the job's model price; the page shows the price used and where it comes from.

**No passage offered** (every quote invented or none given): the prompt says so and the judge still decides each fact from the answer alone;
faithfulness and citation scores stay 0 by construction.

## What the upload accepts and what it tells you (Issues #204 and #203)

A PDF is read in an isolation child by pypdf (strict) and PDFium, and their page counts must agree. A pypdf warning used to make ANY file "corrupt". Now:
- **Listed benign notes are accepted and recorded** on the document version (`document_version_flags`, append-only; the upload response's `flags`, the workspace, app.log `document_flagged`):
  `xref_not_zero_indexed` (the exact pypdf message "Xref table not zero-indexed. ID numbers for objects will be corrected."; the numbering is corrected in memory)
  and `trailing_bytes_after_eof` (non-blank bytes after the last `%%EOF`, only when pypdf itself says nothing about them). The stored bytes and sha256 are the original: nothing is repaired.
- **Any other warning rejects, with its own code** `pdf_parser_warning` ("the PDF can be read, but the reader reported a problem that is not on the list of harmless
  ones"), different from `corrupt_pdf` ("the PDF cannot be read"). The 422 message names the reason in words; the page shows it in zh-TW with the server's message in brackets.
- **No text layer (a scan)**: a bounded PDFium sample (at most 20 pages, evenly spread) finding no text accepts the document with the flag `no_text_layer`; the plan repeats it
  (`no_text_layer`, also when the parse itself found no text) and the page says in zh-TW that the text-only pipeline cannot use it (no OCR). A failed sample means "not measured", never a rejection.
