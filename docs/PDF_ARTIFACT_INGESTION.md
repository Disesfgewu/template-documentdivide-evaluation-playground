# Local PDF Ingestion into the Artifact Store (#7, Refs #7)

`lab_parser.artifacts.ingest_pdf_artifact(source, store, ...)` ingests an original PDF from a local path into the content-addressed artifact store without holding the file in memory (SPEC R01, 10.2, 16.6). It is the thin PDF-specific layer over `LocalArtifactStore.put_stream` (`db/lab_db/storage.py`).

## One publisher

There is a single publisher for the `sha256/<xx>/<sha256>` layout: `LocalArtifactStore`. Staging, hashing, the pre-publication re-check of the staged file, no-clobber publication, verification of an already stored object, the canonical-path check and cleanup all live in `put_stream` (and `put`, which shares the verification helpers). This module keeps only what is specific to a local PDF:

- opening the source with `O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC` and refusing missing paths, directories, FIFOs and symlinks;
- the PDF check, run as the store's `inspect` hook on the staged bytes before anything is published, so a rejected PDF never reaches the store. Cheap checks first (size, empty, `%PDF-` header within 1024 bytes), then the bytes go to `lab_parser.isolation.inspect_geometry` (#51): a separate Linux process under RLIMIT memory/CPU/file-size and a wall deadline (limits scale with file size inside the isolation module's bounds), strict `pypdf`, encryption rejected without password guessing, `max_pages`, and the PDFium page-count cross-check, so `page_count` is the number pypdf, the geometry extractor and the renderer agree on (a `/Count` that lies about `/Kids` is rejected). Child failures map to fixed codes: `corrupt_pdf` (including any pypdf warning such as a repaired xref), `encrypted_pdf`, `page_limit_exceeded`, `inspection_timeout`, `inspection_failed`. There is no flagged-publish path (`allow_flagged` was removed); repaired PDFs are rejected until a review flow exists;
- mapping store errors to fixed, sanitized codes (`file_too_large`, `artifact_corrupted`, `artifact_root_unavailable`, `artifact_store_failed`, `publication_durability_unknown`); no PDF bytes, parser diagnostics or paths are ever included.

The parser does not import `lab_db`: the caller passes the store (`ingest_pdf_artifact(path, LocalArtifactStore(root))`).

## `LocalArtifactStore.put_stream(source_fd, *, max_bytes, inspect=None)`

- Reads at most `max_bytes + 1` bytes; a longer source raises `StreamTooLarge` and nothing is published.
- Stages privately (`0600`, `sha256/.stage-*`), hashes while writing, fsyncs.
- `inspect(stage_fd, sha256, size)` may reject by raising; it runs before publication.
- Before publishing, re-checks the staged descriptor against its directory entry (same inode, one link, mode `0600`, size) and re-hashes it, so a rewritten or swapped stage file fails with `StorageCorruption`.
- Publishes with `os.link` (never overwrites). If the object already exists it is verified (`reused_existing=True`) or rejected as corrupted; the final canonical-path check is the same as `put`.
- Zero or negative writes fail immediately; positive short writes continue until the chunk is complete. Verification readers close even if seek or stream construction fails.
- If the final link exists but its directory fsync fails, `PublicationDurabilityUnknown` maps to `publication_durability_unknown`. The immutable object is preserved, no success is returned, and a retry verifies/reuses the same content identity; this error does not mean publication never occurred. If staging cleanup also fails, the primary uncertainty remains with a fixed cleanup note; descriptors still close and the leftover stage requires cleanup.
- The stage file is removed on every path; a failure to remove it is reported (`StorageCorruption`).

## Where the earlier fault-injection cases are covered now

The earlier stage-tampering, publication-failure, stage-permission and descriptor-leak tests exercised code that no longer exists in this module. They moved to the store, in `tests/dbunit/test_storage.py::PutStreamTests`: in-place rewrite, replaced inode, extra link or wrong mode of the stage file, corrupted existing destination never clobbered, symlinked `sha256` directory refused, stage-removal failure reported and descriptors closed, no descriptor growth across 30 failures, concurrent duplicate writers publish one object, limit boundary and read bound, invalid arguments. PDF-level cases stay in `tests/parser/test_artifacts.py`. Regressions also cover stalled/short writes, repeated seek/fdopen failures without descriptor growth, and post-link fsync uncertainty at both store and PDF boundaries.

## Usage

```python
from pathlib import Path
from lab_db.storage import LocalArtifactStore
from lab_parser.artifacts import ingest_pdf_artifact
from lab_parser.importer import Limits

store = LocalArtifactStore(Path("/var/lib/lab/artifacts"))
artifact = ingest_pdf_artifact(
    Path("/path/to/source.pdf"),
    store,
    limits=Limits(max_bytes=512 * 1024 * 1024, max_pages=2000),
)
```

## Limitations

- Local filesystem only (`Refs #7`): no HTTP upload (already on main), no PostgreSQL registration, lineage/audit/outbox events (#5), workflow jobs or traces (#6), and not batch parsing.
- Directory modes follow `LocalArtifactStore` (not set by this module); objects are `0600`.
- A stage file left by a crash stays in `sha256/.stage-*` and is never published; there is no sweeper yet.
- Inspection CPU/RAM/time is bounded by the isolation child (resource containment, not a filesystem or network sandbox). The caller still owns admission, authorization and scheduler concurrency; the staged bytes are read into the caller's memory once (up to `max_bytes`) before being handed to the child.
