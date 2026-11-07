# Local PDF Artifact Ingestion Foundation (#7, Refs #7)

`lab_parser.artifacts.ingest_pdf_artifact` provides a bounded, content-addressed local filesystem primitive for ingesting original PDF bytes under an existing absolute `artifact_root` directory (SPEC R01, 10.2, 16.6).

## Scope and Behavior

- **Bounded regular-file streaming**: Opens the local source path with `O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC`, rejects missing paths, directories, FIFOs, and symlinks, and streams at most `limits.max_bytes + 1` bytes into a private `0600` stage file (`.stage-pdf-*`) under `artifact_root` while computing the raw SHA-256 digest. Over-limit inputs fail with `file_too_large` before any PDF parsing.
- **Strict PDF and page-limit validation**: Validates the staged descriptor using the same strict `pypdf` inspection order as `lab_parser.importer.inspect_pdf` (empty, `%PDF-` header within the first 1024 bytes, strict structure parsing, encryption rejection without password guessing, `max_pages` ceiling, and `xref_repaired` warning detection). By default (`allow_flagged=False`), `xref_repaired` PDFs are rejected; callers may pass `allow_flagged=True` to permit flagged PDFs while preserving warning diagnostics.
- **Tampering and symlink defense**: Re-verifies the staged file descriptor and directory entry (`st_dev`, `st_ino`, size, and SHA-256) immediately before publication so in-place modifications or stage path replacement fail closed with `stage_tampered`. Subdirectories `sha256/<prefix>` are created with mode `0700` and opened with `O_DIRECTORY | O_NOFOLLOW`.
- **No-clobber content-addressed publication**: Publishes the staged file to `sha256/<sha256[:2]>/<sha256>` (`object_version="1"`) via Linux `renameat2(..., RENAME_NOREPLACE)` and fsyncs the target directories. If the destination already exists (including under concurrent duplicate writers), the existing file is never overwritten; it is opened with `O_NOFOLLOW` and verified against the expected SHA-256 and byte length (`reused_existing=True`), or rejected with `artifact_corrupted` on any mismatch.
- **Failure cleanup**: Any validation, tampering, or publication failure unlinks the unpublished private stage file; if unlinking fails, `cleanup_failed` is raised without leaking raw PDF bytes, parser internals, or filesystem paths.

## Usage

```python
from pathlib import Path
from lab_parser.artifacts import ingest_pdf_artifact
from lab_parser.importer import Limits

artifact = ingest_pdf_artifact(
    Path("/path/to/source.pdf"),
    Path("/var/lib/lab/artifacts"),
    limits=Limits(max_bytes=512 * 1024 * 1024, max_pages=2000),
)
```

## Limitations

This increment is a local filesystem primitive only (`Refs #7`):
- It does not provide HTTP upload endpoints (`/api/v1/documents`), PostgreSQL transactions, `artifacts` / `document_versions` / `pages` table registration, lineage/audit/outbox events (#5), or workflow jobs/traces (#6).
- It requires Linux (`renameat2` `RENAME_NOREPLACE`) and a trusted, exclusively controlled `artifact_root` directory.
- Byte and page limits bound file size and page count, not malicious-parser CPU/RAM consumption or process isolation.
