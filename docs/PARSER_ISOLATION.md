# Resource-limited PDF geometry inspection

`lab_parser.isolation.inspect_geometry` runs the merged `extract_pages` geometry
reader on real PDF bytes in a separate Linux process. It returns immutable,
zero-based physical page indexes, rotation, visible display width/height, input
SHA-256 and byte size. It does not extract text, render images, perform OCR,
verify document facts or publish parser capability readiness. Dimensions remain
unverified parser observations. Printed labels are deliberately excluded.

```python
from lab_parser.isolation import ProcessLimits, inspect_geometry

result = inspect_geometry(pdf_bytes, ProcessLimits(wall_seconds=10))
assert result.page_count == len(result.pages)
```

The caller supplies already authorized, bounded bytes and owns concurrency,
durable job state, admission, artifacts and trace records. The optional
`cancelled` callback must be fast, nonblocking and safe to call repeatedly.
Failures raise `InspectionError` containing a fixed code only. Parser warnings
cause rejection instead of silently publishing repaired geometry. Encrypted,
malformed, empty and excessive-page PDFs do not produce partial results.

The default `ProcessLimits` profile is versioned by the calling operation:
512 MiB input, 2000 pages, 512 MiB virtual address space, 5 CPU seconds, 10 wall
seconds and 1 MiB output. Limits are explicit immutable values; changed profiles
must be recorded by the operation owner. The child sets Linux `RLIMIT_AS`,
`RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_CORE=0` and `RLIMIT_NOFILE=64` before importing
pypdf. CPU/address-space limits cover that child, not aggregate deployment RSS.
A signal produces `child_failed`; signals alone do not prove an OOM diagnosis.
An actual caught `MemoryError` produces `memory_limit`. Lower inherited hard
limits may prevent bootstrap and cause `child_failed`.

The parent launches a fixed companion using its trusted interpreter with `-I`,
a minimal environment, no inherited extra file descriptors and a new process
session. `-I` excludes caller `PYTHONPATH` and user-site, while the installed
interpreter/virtualenv and its startup files remain trusted. Interpreter startup
precedes the resource bootstrap. No command, code, package path or output path
comes from the PDF. Stdout/stderr are discarded, and parser warning detection
never formats or retains warning messages. Input/output files are private in a
0700 temporary directory and are removed after process cleanup. The result file
is bounded and must be regular, not a symlink; JSON must have exact fields,
finite dimensions, contiguous indexes and matching input identity.

Wall timeout and cancellation are checked before launch, during child execution
and before publication, including after validation. The process group is killed
before the leader is reaped, preventing PID reuse between exit observation and
group cleanup. The direct child is reaped on success, failure, timeout and caller
exception. A blocking host filesystem or callback cannot be forcibly interrupted
by this synchronous function; the wall setting does not promise a hard bound on
parent hashing, local I/O, callback or cleanup. Parent input is already resident
and bounded. Temporary disk capacity, scheduling and host limits are deployment
obligations.

This boundary is **not a filesystem or network security sandbox**. The child runs
as the same OS user and can access that user's files and network. Process-group
cleanup covers descendants that remain in that group; hostile code can escape
with a new session. Grandchildren are terminated but reaped by their adopting
parent, not this function. It is for the
fixed trusted parser, not uploaded programs. Deploy hostile-document processing
with a dedicated low-privilege identity, filesystem/network isolation and cgroup
or container aggregate limits. Parent API/worker wiring, cancellation propagation,
DB/lease/trace integration, OCR/vision/parser profiles, resource measurements on
500/600-page documents and release acceptance remain open in #14 and its parent
dependencies. This foundation does not complete T81/T82/T104 or any end-to-end A
condition.

Validation: `scripts/check.sh --scope 'parser governance'` runs real synthetic
PDF inspection and negative input tests, plus controlled dependency fault probes
for actual bootstrap CPU, address-space and output limits. Fault probes use
explicit fakes; they are not real-parser quality or performance evidence. Tests
also cover cancellation, wall expiry, strict result validation, diagnostic
secrecy, environment/FD exclusion and temporary cleanup. Runtime dependencies
are unchanged; the existing pinned pypdf environment is used.

## Page-count agreement with the renderer

The child counts pages with pypdf (which follows `/Kids`) and then opens the same
bytes with PDFium (which trusts `/Count`). A file whose `/Count` disagrees with
`/Kids` gives different counts (for example three pages for pypdf and one or
ninety-nine for PDFium), so geometry from pypdf would describe pages the
renderer does not see. The child therefore requires the pypdf page count, the
number of extracted geometry rows and the PDFium page count to be equal, and
otherwise reports `invalid_pdf` (no geometry is published, no new error code).
The check opens the bytes with PDFium inside the same resource-limited process.
A PDF that pypdf accepts but PDFium cannot open is rejected as `invalid_pdf` too.
Regressions use hand-built PDFs with a correct xref (`/Count` below and above
`/Kids`, one page object listed three times) and an honest control. Related
Issue #7. The non-zero exit versus signal distinction (resource kills) is
unchanged and still reported as `child_failed`.
