# Visual evidence publication eligibility

`contracts/lab_contracts/verification.py` implements the local, read-only
`visual-publication-gate-1` policy. It returns eligibility plus located reason
codes and accepted verification IDs. It never changes evidence, marks an
artifact verified, freezes a benchmark or makes a provider call.

This independent #9 foundation uses the merged `EvidenceUnit` and
`SourceRegion` contracts. It does not depend on the pending parser, vision,
context-linker, derivation, database or benchmark-planning branches. It covers
structural portions of R27/R29/R30 and T100/T102, not complete numbered tests,
A44/A48, genuine human verification or release acceptance.

## Inputs and use

A `VisualSubject` identifies an immutable subject/revision and all producer
operations. Its document scope pins logical IDs, original PDF hashes and page
counts. Every evidence region must match that scope. Every visual evidence
must link an original image manifest entry through its existing `asset_refs`.
An image may support several evidence units. Cross-page evidence keeps every
constituent region; the gate does not merge coordinates or synthesize geometry.

Call `subject_hash(subject)` before sending this exact snapshot for independent
verification. The hash includes gate revision, subject revision, producer IDs,
document identities, evidence contents/regions, required image IDs/hashes/sizes,
risk classification and unresolved state. It hashes a normalized typed JSON
snapshot with sorted keys; it is not a hash of raw parser JSON or an authenticity
signature. Changing any snapshot field invalidates an earlier review. The
original PDF/image SHA-256 values retain their separate byte identity.

`check_visual_publication(subject, records, read_image, authorize_verification)`
revalidates and copies inputs before invoking external callbacks. Its result
contains the snapshot hash so a transactional caller can reject a concurrent
revision change before publication. The gate does not mutate input lists. Its
records are internal Python contracts, not new public API/schema exports.

`read_image(artifact_id, maximum_bytes)` must read committed original image bytes
from authorized storage scoped to the subject's project. The bound is expected
size plus one byte; the storage adapter must enforce it before allocating or
reading arbitrary payloads. The gate rejects unavailable/non-byte/empty/wrong-size
or wrong-hash results. It does not resolve paths or URLs and does not decode
images. Decodability, MIME, render/crop-to-region provenance, necessary labels
and semantic image quality must already be checked by the trusted artifact and
verifier services. A matching byte hash alone does not prove an original crop
or that anybody actually read an image.

`authorize_verification(record)` must return exactly `True` only after checking
record authenticity, project scope and authorized oracle/reviewer roles against
trusted storage/identity services. The callback receives a defensive copy;
errors, false and truthy non-boolean values block eligibility. A supplied
`oracle="human"`, reviewer ID or timestamp is not identity proof. Never pass
model output or untrusted request records directly, or use an allow-all callback
in production. The deterministic tests use explicit fake callbacks and do not
claim authentication integration.

## Decisions and human gates

Each accepted record must bind the current snapshot hash, cover exactly all
required evidence/image IDs and risks, and use an operation ID distinct from
all producer operations. A distinct provider/model is unnecessary, but a
separate persisted call/input record is required. The caller must authenticate
that call and its input; an arbitrary new operation string proves nothing.

The trusted classifier must provide all applicable risk flags: estimated values,
cross-page cell merges, conflicts, dual axes, logarithmic axes, image/text
conditions and causal claims. Each requires a passing authorized human review
of the current snapshot. Confidence cannot bypass this requirement. A verifier
can report `additional_risks`; a newly reported risk blocks the old snapshot
and must be included in a new subject revision before re-verification. The gate
cannot infer all semantic risks from raw strings and does not replace independent
classification, visual necessity/ablation testing or benchmark human sampling.

Unresolved evidence always blocks eligibility, including after a human pass.
Any supplied current-revision fail/needs-review record blocks eligibility even
when another record passes. Stale, unauthorized, incomplete or self-attested
records also block; they are never silently ignored. The caller must supply the
complete current-snapshot record history from trusted storage, including negative
records; selecting only passing records is forbidden. Corrections create a new
revision, retain old history, and obtain new verification rather than overwriting
an earlier decision.

## Bounds and remaining integration

At most 128 documents, evidence units, image requirements or verification records
are accepted. Each evidence has at most 128 regions/asset refs. Each original
image is nonempty and at most 16 MiB, all images total at most 64 MiB. Evidence
content/quotes total at most 8 MiB; each evidence's raw+normalized content is at
most 1 MiB. The normalized subject snapshot is at most 16 MiB. Strict IDs,
counts, booleans, finite geometry and aware timestamps are validated. The gate
performs no retries. Loader/authorization exception messages are omitted from
results to avoid leaking credentials or private storage details.

Caller-supplied functions execute synchronously; their transport timeout,
cancellation, memory enforcement and authorization remain adapter obligations.
This policy primitive is not a database, sandbox or authorization server.
Transactional publication with revision/asset rechecks, project FKs,
audit/outbox/lineage, immutable persistence, graph integration, parser/vision
execution, authenticated review UI, real fixture/human quality evidence and the
full #8/#9/#10 acceptance remain open. Original image reads must be coupled to
an immutable storage version or publication transaction; an eligibility result
cannot authorize a later changed subject or artifact.

Run deterministic checks with:

```sh
PYTHONPATH=contracts python -m unittest tests.contracts.test_visual_verification -v
scripts/check.sh --scope 'contracts fixtures governance'
```
