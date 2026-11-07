# Deterministic PDF Page-Batch Planning and Coverage Accounting

`services/parser/lab_parser/batch_coverage.py` implements the deterministic, bounded page-batch planning and per-page coverage accounting foundation for Issue #7 (SPEC 2, 4.2, 16.6, 19.1, 19.4). It does not execute external parsers, model providers, databases, or queues, and never fabricates parsed content or evidence.

```bash
PYTHONPATH=services/parser python3 -m tools.governance.run_tests tests/parser
scripts/check.sh --scope "parser governance"
```

## Limits and Deterministic Partition (`plan_batches`)

- **Validated positive finite limits (`BatchLimits`)**:
  - `max_pages`: integer in `1 .. 2000` (default `2000`, SPEC 16.6).
  - `batch_size`: integer in `1 .. max_pages` (default `10`, SPEC 16.6).
  - `overlap_pages`: integer in `0 .. batch_size - 1` (default `1`, SPEC 19.4).
  - `parallel_batches`: integer in `1 .. max_pages` (default `2`, SPEC 16.6).
  - Non-integers, booleans, zero/negative limits, `overlap_pages >= batch_size`, or `page_count > max_pages` fail closed with `ValueError`.
- **Core partition and adjacent overlap**:
  - Every physical page in `0 .. page_count - 1` is assigned as a `core` page (`is_core=True`) to **exactly one** batch in strictly increasing order.
  - Each batch includes up to `overlap_pages` preceding (`overlap_before`) and succeeding (`overlap_after`) pages as candidate context while preserving their original 0-based physical `page_index` alongside `batch_page_offset`.
  - `is_full_document` is `True` only when a single batch's core pages cover the entire document (`len(core_page_indices) == page_count`), preventing partial batches from masquerading as full-document inputs (SPEC 19.1, 19.4, T97).
  - `plan_id`, `plan_sha256`, `batch_id`, and `batch_sha256` are derived deterministically from canonical JSON over `BATCH_PLAN_RULE_VERSION` (`pdf-batch-plan-1`), document identity/SHA-256, `page_count`, and `BatchLimits`.

## Coverage Accounting and Duplicate/Conflict Handling (`evaluate_coverage`)

- **Per-page core coverage invariants**:
  - Each physical page's coverage status is determined solely by its unique core-owning batch.
  - Context overlap pages (`overlap_before` / `overlap_after`) are recorded in `overlap_observations` for boundary diagnostics, but **never** satisfy core coverage for a missing, failed, `partial`, or `needs_review` core page in its owning batch.
  - Conversely, a failed overlap observation in a neighbor batch does not invalidate an `ok` core result in the page's owning batch.
  - Every non-`ok` page report (`partial`, `needs_review`, `failed`) must carry non-empty `diagnostics`.
- **Completion and review gates**:
  - Any `failed` or `missing` core page forces `status="incomplete"`, `can_claim_complete=False`, and marks those pages (`retry_page_indices`) and their owning batches (`retry_batch_ids`) for retry.
  - Any `partial` or `needs_review` core page (with no `failed`/`missing` pages) transitions `status="needs_review"` and sets `can_claim_complete=False`.
  - Only when every core page in `0 .. page_count - 1` is reported `ok` by its owning batch does the report reach `status="complete_raw"` and `can_claim_complete=True`. `can_freeze_benchmark` is always `False` at the page-batch stage.
  - `select_retry_batches(plan, report)` validates full structural and cryptographic consistency of both `plan` (including `batch_count`) and `report` (including per-page statuses/flags, summary lists, `retry_batch_ids`, `completed_batch_ids`, `missing_batch_ids`, `coverage_ratio`, and `report_sha256`) before returning batches with `failed` or `missing` core pages, reusing completed and `needs_review`/`partial` batches without re-running them (SPEC 4.2, T07).
- **Duplicate, conflicting, and out-of-plan results**:
  - Exact duplicate `BatchResultInput` submissions for the same `batch_id` (even with page reports in a different list order) are idempotently deduplicated.
  - Conflicting duplicate submissions for the same `batch_id` (differing page statuses, diagnostics, roles, or `operation_id`) fail closed with `ValueError("conflicting_duplicate_batch_result: ...")`.
  - Unknown `batch_id`, mismatched `batch_index`, pages outside the batch's planned `window_page_indices`, duplicate `page_index` within a single batch report, or a page report whose explicit `role` disagrees with its planned role (`core` vs `overlap_before`/`overlap_after`) fail closed with `ValueError`.

## Scope and Remaining Gates

`tests/parser/test_batch_coverage.py` exercises 500, 600, and 2,000 page plans, overlap boundaries, overlap-vs-core isolation, duplicate/conflicting/out-of-plan rejection, and retry batch selection. These are deterministic foundation unit/negative tests (Refs #7; R01, R02, R07, R28; T03, T07, T14, T94, T97 subcases). Upload/API endpoints, real parser execution (#8), full-document context linking (#9), durable PostgreSQL/job/checkpoint persistence (#5, #6), and full end-to-end 500-page recovery (T81) remain open.
