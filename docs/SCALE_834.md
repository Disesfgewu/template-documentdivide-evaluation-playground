# The whole flow at 834 pages (Issue #205 / #204 PM item 2)

`LAB_SCALE=1 LAB_TEST_DATABASE_URL=... python -m unittest tests.api_db.test_scale_834_walk_real_db` walks upload -> parse result (78,396 evidence units, 94 per page,
the density measured on the owner's real 834-page manual) -> plan/promote -> estimate -> generate (30 questions, fake model) -> validate -> review -> freeze ->
questions-only export -> developer upload (24 answers, quotes located over 834 pages) -> rule scoring -> judge -> results, and prints the time and payload per step.
The model is the fake lab-gen; the parser is a synthetic one at that density; the database is a local PostgreSQL. Times are for this machine and show orders of magnitude.

| step | before the fixes | after |
|---|---|---|
| store the parse result (78,396 units) | 3.3 s | 3.4 s |
| **POST plan (promote the parse: write all evidence units)** | **90.7 s** (the web page gives up at 60 s) | **14.1 s** |
| GET plan / progress / workspace | 0.28 / 0.02 / 0.03 s, 4.7 / 0.5 / 0.9 KiB | same |
| GET estimate (generate) and POST generate | 2.6 s each (the clean pages are read and validated: 78k units) | same |
| worker: generate 30 questions / validate / judge (fake model) | 5.7 / 0.3 / 0.3 s | same |
| **which pages the 30 questions come from** | **the first 24 (0..23): title, contents, preface** | **spread: 0, 34, 69, ... 764, 799** |
| GET review (24 questions) / export / results / submission-details | 64 KiB / 13 KiB / 16 KiB / 84 KiB | same |
| developer upload, 24 answers, quotes found in 834 pages | 1.9 s (one pass over the document's units) | same |
| **judge estimate for the uploaded file (3 / 15 USD per MTok)** | **7.31 USD: over the 5 USD ceiling, the judge could not start** | **2.15 USD** |

Findings fixed in this PR:
1. **Promote wrote four rows per unit with a round trip each** (78,000 units, 91 s inside one request). It is now a few statements per page (`executemany`, one lookup of the
   existing keys per page, one advisory lock per promotion): 14 s. The rest is mostly the strict re-validation of every unit.
2. **Page selection took the first pages.** `select_pages` was round-robin from page 0 and `select_page_pairs` took the first pairs, so a small `question_count` on a long document
   only covered its opening pages. Both are now deterministic and evenly spread (whole passes first when more questions than pages).
3. **The judge estimate of an uploaded file assumed 72 KB of passages per call.** It now uses the real size of each submission's found passages (they are in the database),
   still a worst-case bound (a test checks it is at least what the real prompts cost) and 3.4 times lower here.

Observed, not changed:
- Nothing per question loads the whole document: scoring reads only the gold units' regions; the upload reads the document's units once (1.9 s).
- Generate/estimate/POST generate each re-read and re-validate all clean pages (2.5 s at 78k units): bounded per request, not per question.
- Lists: the review is 64 KiB for 24 questions and the per-question details 84 KiB, linear in the question count the owner asks for (20-40); the plan is 4.7 KiB (it
  summarises pages, it does not list units). A 500-question benchmark would make the review about 1.3 MB: paging it is the follow-up if that size is ever used.
- Peak memory of the walk process: about 1.2 GiB, almost all of it the synthetic whole-document parse held in memory by the test itself.
