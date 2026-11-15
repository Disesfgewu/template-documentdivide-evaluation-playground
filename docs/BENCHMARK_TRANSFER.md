# Private benchmark transfer

`lab_contracts.transfer` exports and verifies a bounded JSON transfer package for
operator-controlled storage. It preserves the four separate benchmark parts,
joined by question ID, and snapshots evidence and validation records. The
separate participant projection returns only `question_id`, `question_text` and
`language`, ordered by the question's original ordinal.

This foundation implements the structural transfer portion of SPEC R04/R07/R23,
T12/T13/T78 and the projection boundary of R19/R29. It does not establish
semantic validity, authorize a caller, select an accepted frozen revision, write
to PostgreSQL, record audit/outbox events or serve a UI/API. Those remain under
Issues #5/#6/#10/#13. A successful integrity check is not freeze approval or
proof of a trusted author. Parent and release gates remain open.

## Source and package format

An operator source is one UTF-8 JSON object with exactly these keys:

- `metadata`: `benchmark_id`, `revision_id`, `document_id`, `document_sha256`,
  `page_count` (strict integer 1–2000).
- `files`: six arrays named `questions`, `semantic_answers`, `textual_answers`,
  `source_answers`, `evidence`, `validations`, using the existing v1 contracts.
- `source_manifest`: null, or an existing `BenchmarkManifest` unchanged. The codec
  never invents `frozen_at`, quality/compliance, configuration versions or a
  verification decision. A supplied manifest must match identity, actual
  question/bucket counts and the canonical four-part file hashes.

The package adds `format: "lab-benchmark-transfer-1"`,
`canonicalization: "lab-c14n-2"` and an `inventory` containing SHA-256, byte length
and record count for each logical file. These names are not filesystem paths;
there is no ZIP/TAR extraction or archive-supplied path handling. Each logical
file is the canonical JSON array without a trailing newline. Arrays are sorted
by their record ID during export; nested arrays, text, units and source number
values retain their original content. A strict integer whose canonical
representation would become exponent notation is rejected so every exported
package remains importable under the unchanged strict-integer contracts. Canonicalization is the repository's
Python-defined policy, not RFC 8785. Exact decimal JSON numbers are kept for
hashing and roundtrip rather than rewritten through binary64 model dumps.

Exact coordinate/confidence bounds, positive source area and numeric interval
ordering are checked on the preserved decimal values as well as through the
existing binary64 contract models, so rounding cannot admit values outside
the original numeric bounds.

Import checks the complete inventory and every record schema before returning
records, then checks cross-part references, duplicates, evidence document
ID/hash and zero-based page bounds. Existing model validators are structural;
they cannot prove a chart was read, a source supports an answer or a recorded
human review actually happened. Asset references remain references: source PDF,
image bytes, parser/provider payloads and their availability are not included or
verified. This is not a complete backup/restore artifact.

A returned snapshot is mutable but detached. Editing it requires revalidation
and a new resource revision according to service policy; this codec does not
permit overwriting a stored frozen revision. Hashes detect corruption but do
not authenticate a package. Trusted storage, signatures where required and
caller authorization are service responsibilities.

## Callable API and CLI

The public Python entry points accept bytes:

```python
from lab_contracts.transfer import export_archive, import_archive, participant_questions

private_package = export_archive(operator_source_json_bytes)
operator_snapshot = import_archive(private_package)
public_questions_json = participant_questions(private_package)
```

Run with the project's locked contract dependencies installed:

```bash
PYTHONPATH=contracts python -m lab_contracts.transfer export source.json --output private-package.json
PYTHONPATH=contracts python -m lab_contracts.transfer check private-package.json
PYTHONPATH=contracts python -m lab_contracts.transfer import private-package.json --output restored-source.json
PYTHONPATH=contracts python -m lab_contracts.transfer project private-package.json --output questions.json
```

Input must be a regular file; the final input symlink and FIFOs are rejected.
Reads are bounded even if the file grows after its size check. Output is created
exclusively with mode 0600 and will not overwrite an existing file or final
symlink. Parent directories must be operator-controlled. Diagnostics contain
stable failure codes, never validation payloads, paths or gold values. Exit 0
means the requested local operation passed; exit 2 means rejected data or file
I/O failure. CLI argument errors also use argparse's exit 2.

All JSON inputs and generated outputs are limited to 16 MiB, 20,000 records
across the six files, depth 64 and 500,000 JSON structure nodes. A conservative
pre-parse token/depth scan bounds nesting and dense arrays before JSON object
allocation; a tree walk bounds decoded nodes and text before serialization.
These limits are transfer limits, not a measured deployment RSS/latency SLA.

## Participant boundary

Only the explicit `participant_questions` entry point produces the minimal
public projection; the private archive and imported snapshot contain gold and
must never be offered to a participant. The projection omits internal evidence,
visual objects, quota classifications, private hashes and verifier records.
Filtering fields cannot detect answers or internal identifiers accidentally
written inside `question_text`: authoring validation and human gates must check
that content before the service authorizes export. The caller must enforce
project/role permissions and select an approved frozen revision. The codec has
no authentication context and must not be exposed as an unguarded HTTP route.

## Verification

```bash
PYTHONPATH=contracts python -m unittest tests.contracts.test_transfer -v
scripts/check.sh --scope 'contracts fixtures governance'
```

Tests use the existing hand-written synthetic contract bundle, not real
parser/provider outputs or an accepted human-reviewed benchmark. They exercise
roundtrip, ordering, exact source preservation, all-file inventory corruption,
rehashed invalid records, IDs/source/page checks, size/nesting limits,
projection allowlisting and actual CLI private-file handling. No full T78,
A18/A20/A47, DB/API/UI or release acceptance is claimed by this utility.
