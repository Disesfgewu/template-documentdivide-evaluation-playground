# Local PDF Ingestion into the Artifact Store (#7, Refs #7)

`lab_parser.artifacts.ingest_pdf_artifact(source, store, ...)` ingests an original PDF from a local path into the content-addressed artifact store without holding the file in memory (SPEC R01, 10.2, 16.6). It is the thin PDF-specific layer over `LocalArtifactStore.put_stream` (`db/lab_db/storage.py`).

## One publisher

There is a single publisher for the `sha256/<xx>/<sha256>` layout: `LocalArtifactStore`. Staging, hashing, the pre-publication re-check of the staged file, no-clobber publication, verification of an already stored object, the canonical-path check and cleanup all live in `put_stream` (and `put`, which shares the verification helpers). This module keeps only what is specific to a local PDF:

- opening the source with `O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC` and refusing missing paths, directories, FIFOs and symlinks;
- the strict PDF check (`inspect_pdf` order: empty, `%PDF-` header within 1024 bytes, strict `pypdf` structure, encryption rejected without password guessing, `max_pages`, `xref_repaired` detection). It runs as the store's `inspect` hook on the staged bytes, before anything is published, so a rejected PDF never reaches the store. `allow_flagged=False` (default) rejects `xref_repaired` PDFs;
- mapping store errors to fixed, sanitized codes (`file_too_large`, `artifact_corrupted`, `artifact_root_unavailable`, `artifact_store_failed`); no PDF bytes, parser diagnostics or paths are ever included.

The parser does not import `lab_db`: the caller passes the store (`ingest_pdf_artifact(path, LocalArtifactStore(root))`).

## `LocalArtifactStore.put_stream(source_fd, *, max_bytes, inspect=None)`

- Reads at most `max_bytes + 1` bytes; a longer source raises `StreamTooLarge` and nothing is published.
- Stages privately (`0600`, `sha256/.stage-*`), hashes while writing, fsyncs.
- `inspect(stage_fd, sha256, size)` may reject by raising; it runs before publication.
- Before publishing, re-checks the staged descriptor against its directory entry (same inode, one link, mode `0600`, size) and re-hashes it, so a rewritten or swapped stage file fails with `StorageCorruption`.
- Publishes with `os.link` (never overwrites). If the object already exists it is verified (`reused_existing=True`) or rejected as corrupted; the final canonical-path check is the same as `put`.
- The stage file is removed on every path; a failure to remove it is reported (`StorageCorruption`).

## Where the earlier fault-injection cases are covered now

The earlier stage-tampering, publication-failure, stage-permission and descriptor-leak tests exercised code that no longer exists in this module. They moved to the store, in `tests/dbunit/test_storage.py::PutStreamTests`: in-place rewrite, replaced inode, extra link or wrong mode of the stage file, corrupted existing destination never clobbered, symlinked `sha256` directory refused, stage-removal failure reported and descriptors closed, no descriptor growth across 30 failures, concurrent duplicate writers publish one object, limit boundary and read bound, invalid arguments. PDF-level cases stay in `tests/parser/test_artifacts.py`.

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
- Byte and page limits bound size and page count, not malicious-parser CPU/RAM or process isolation.
