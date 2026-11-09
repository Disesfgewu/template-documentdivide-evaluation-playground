# Explicit full-document context candidates

`services/parser/lab_parser/context_linker.py` implements the independently
usable, deterministic candidate stage of SPEC 19.4 (Refs #9). It links distant
pages without an adjacency window or embedding threshold. It does not establish
that the PDF actually supports a relation. No result has verified status.

Run from the repository root with parser dependencies installed:

```sh
PYTHONPATH=services/parser python -m lab_parser.context_linker manifest.json > candidates.json
scripts/check.sh --scope 'parser contracts governance'
```

The same operation is available as `load_manifest(payload)` followed by
`link_document(document, evidence)`. The CLI accepts a regular file (including
a symlink to one), rejects special files without blocking, reads at most 16 MiB
plus one byte, and exits 2 with a fixed diagnostic for invalid input. Exit 0
means candidate generation completed, not that evidence was verified. Quotes,
labels and all other input text are data; the operation makes no model calls,
executes no document instructions and performs no network or database access.

## Internal format

Input schema version is `context-link-input-1`; output is
`context-link-result-1`; algorithm revision is `explicit-context-linker-1`.
Unknown/missing fields, duplicate JSON keys, non-finite numbers, invalid physical
pages or geometry are rejected. This is an internal format, not a new LAB API or
an upstream parser schema. Python dataclasses in the module define its fields.
The following complete example links page 0 to page 1; null qualifiers preserve
unknown scope and therefore produce an unresolved candidate:

```json
{
  "schema_version": "context-link-input-1",
  "document": {
    "project_id": "project-1",
    "document_id": "pdf-1",
    "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "document_revision": "1",
    "page_count": 2,
    "processed_pages": [0, 1]
  },
  "evidence": [
    {
      "evidence_id": "figure-1",
      "revision": "1",
      "regions": [{"document_id": "pdf-1", "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "page_index": 0, "bbox": [0.1, 0.1, 0.9, 0.9], "kind": "figure", "quote": null}],
      "anchors": [{"kind": "figure", "label": "1", "chapter": null, "entity": null, "year": null, "unit": null, "condition": null}],
      "references": []
    },
    {
      "evidence_id": "text-1",
      "revision": "1",
      "regions": [{"document_id": "pdf-1", "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "page_index": 1, "bbox": [0.1, 0.1, 0.9, 0.2], "kind": "text", "quote": "See Figure 1"}],
      "anchors": [],
      "references": [{"target": {"kind": "figure", "label": "1", "chapter": null, "entity": null, "year": null, "unit": null, "condition": null}, "relation_type": "refers_to"}]
    }
  ]
}
```

The caller supplies explicitly extracted identifiers; the linker does not infer
them from quotes. Labels and chapter/entity/year/unit/condition qualifiers match
exactly and case-sensitively. A reference's qualifiers describe its **target**
scope, allowing explicit cross-chapter references. Known qualifier disagreement
is `conflicting`; missing qualifiers or multiple compatible destinations are
`unresolved`; a unique fully qualified match is `candidate`. None of these
states proves semantic support, actual contradiction, or source correctness.
Unmatched references are returned separately. Supporting/refuting regions and
reviewer decision remain empty pending independent verification.

Every source/target retains its original PDF regions. Exact overlap repeats of
an evidence ID/revision deduplicate; differing payloads under that identity, or
mixed revisions of one logical evidence ID, fail closed. The full-document
manifest must list every physical page exactly once; this checks supplied
coverage, not whether a parser successfully extracted all facts. A caller must
not label incomplete parsing complete merely to satisfy the manifest.

Context and relation hashes bind project/document revision, source hash, complete
evidence metadata and linker revision. Evidence input order and exact overlap
repeats do not change relation identities. Metadata order inside an evidence
record remains part of its identity. New contexts produce new identities rather
than modifying an older result. Limits are 2,000 pages, 10,000 input records,
32 regions/anchors/references per record, 50,000 examined anchor matches and
16 MiB input evidence/final serialized output. Limits reject instead of silently
truncating. No table cells or source geometry are merged or invented.

## Evidence and remaining gates

`tests/parser/test_context_linker.py` covers deterministic R28 candidate
subcases contributing to T94–T97/A45: distant pages 18/247/300, overlap identity,
scope conflicts, ambiguity, missing metadata, source preservation, strict
manifests and resource/CLI bounds. These are synthetic metadata tests, not F11
quality acceptance or real parser integration. Exact commands/results and commit
are recorded on the PR. The registry/traceability statuses are not promoted.

Still required under #8/#9/#5/#6: parser-derived anchor extraction, batch and
worker integration, persisted relation revisions/trace/audit, original-asset
verification, table boundary/header/cell checks, vision conflicts, independent
verifier/human decisions and F10/F11 quality fixtures. This module is an internal
operation and cannot freeze gold or publish verified relations.
