# Question Generation Cache Identity and Provenance

`contracts/lab_contracts/generation_cache.py` implements the deterministic
`generation-cache-key-1` identity and provenance validation primitive for
question generation (SPEC 6, 10.2, 16, 19.6). It is an independent #10
foundation built on the merged `SourceRegion`, `EvidenceUnit` and
`ParseManifest` contracts and the `lab-c14n-2` canonical JSON encoder.

This module is a pure, stateless validation and canonicalization boundary. It
does **not** implement or claim durable PostgreSQL cache storage, `cache.hit`
trace/operation records, job checkpointing, provider calls, semantic/source
verification or benchmark freeze.

## Bound Inputs and Provenance

A `GenerationCacheInput` requires and cross-validates five components:

1. **Source Set (`source_set_sha256`)** — Single-PDF `SourceDocumentScope`
   (`document_id`, `document_sha256`, `page_count`), reproducible
   `ParseProvenance` (`parser_name`, `package_or_service_version`,
   `adapter_revision`, `output_schema`, `model_revisions`,
   `requested_profile`, `effective_profile`, `fallback_reason`,
   `ocr_languages`, `page_selection`, `render_dpi`, `render_max_megapixels`,
   `postprocess_flags`), and `EvidenceUnit` records. Every evidence region must
   pass `SourceRegion.check_against_document` and lie on a page covered by
   `parse_provenance`. Display-only `page_label` is excluded from the canonical
   source-set hash (SPEC 4.1).
2. **Actual Ordered Context Manifest (`context_manifest_sha256`)** — `manifest_id`,
   pinned `linker_revision`, ordered `ContextSlot` entries (`slot_index` `0..n-1`,
   `evidence_id`, `content_mode`, `truncated`, `included_char_start`,
   `included_char_end`, `included_content_sha256`, `truncation_reason`),
   verified `ContextRelationRef` links, and committed `input_artifacts`
   (`ArtifactDigest` with `artifact_id`, `sha256`, `size_bytes`, `mime_type`).
   Every slot's `included_content_sha256` is verified against the exact raw or
   normalized slice of its `EvidenceUnit`.
3. **Generation Config (`generation_config_sha256`)** — Pinned
   `generator_config_version`, `question_generator_model`,
   `question_generator_prompt_version`, `output_schema_version`, `language`,
   `max_output_tokens`, `temperature`, optional `seed`, applicable
   `vision_reader_model` and `vision_prompt_version`, and bounded extension
   `parameters` (which cannot shadow core fields). Optional `verifier_revision`
   and `selection_rule_version` are validated for pinning when supplied, but
   excluded from `generation_config_sha256` and `cache_key` so verifier-only
   revisions do not invalidate unchanged generation outputs (SPEC 19.6).
4. **Question Target (`target_sha256`)** — `quota_bucket`, `reasoning_type`,
   deduplicated `modality_requirements`, and `question_count`. `cross_page`
   targets require source evidence spanning at least two physical pages;
   `chart_table` and visual reasoning targets require `table` or `image`
   modalities.
5. **Visual Provenance (`visual_provenance_sha256`)** — Required whenever the
   source evidence contains visual regions (`table`, `figure`, `image`, `page`)
   or the target requests visual modalities/buckets. Each `VisualCropProvenance`
   binds `visual_object_id`, `artifact_id`, `image_sha256`, `size_bytes`,
   `dpi`, `pixel_width`, `pixel_height` ($\le 16\text{ MP}$), `crop_bbox`,
   `rotation_degrees`, pinned `transform_id` and `region_refs`, and must match
   `EvidenceUnit.asset_refs` 1-to-1. Each `region_ref` is resolved against its
   evidence unit's canonical region order and identity (`canonical_region_index`
   plus the referenced canonical region descriptor) before hashing, and duplicate
   regions within an evidence unit are rejected, preventing positional index
   collisions when regions are reordered. Conversely, text-only inputs reject
   stray `vision_reader_model` settings.

## Canonicalization and Untrusted Data

- Order-independent collections (`documents`, `parse_provenance`,
  `source_evidence`, regions within an evidence unit, `asset_refs`,
  `relations`, `input_artifacts`, `modality_requirements`, `visual_provenance`)
  are canonically sorted before hashing.
- `context_manifest.slots` preserves its exact sequence (`0..n-1`) because
  prompt context ordering and per-slot truncation change generation inputs.
- `parse_generation_cache_input` runs `strict_loads` (rejecting UTF-8 BOM,
  duplicate keys, `NaN`/`Infinity` and out-of-range numbers) before Pydantic
  validation, preserves exact `Decimal` numbers inside
  `generation_config.parameters` (top-level and nested) so high-precision JSON
  numbers such as `9007199254740992.0` vs `9007199254740993.0` never collide
  under binary64 rounding, and rejects lossy decimal tokens on fixed binary64
  float fields. `build_generation_cache_key` re-validates Python inputs so
  in-place mutations or `model_construct` callers cannot bypass checks.
- Untrusted PDF text, quotes and parameter strings are serialized strictly as
  JSON string values and never interpreted as policy or instructions.

## Verification and Remaining Dependencies

Run the deterministic suite with:

```bash
PYTHONPATH=contracts python3 -m unittest tests.contracts.test_generation_cache -v
scripts/check.sh --scope 'contracts fixtures governance'
```

This increment covers deterministic contract/unit aspects of R07/R08/R30 and
T22/T23/T101 invalidation rules. Full #10 acceptance still requires #5/#6
durable cache/operation/checkpoint persistence (`cache.hit` logging without
re-billing), #9 semantic/visual necessity verification and human gates, real
LLM generation, and immutable revision freeze.
