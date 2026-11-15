# Local L0 render bundles

The standalone Linux operator command makes the existing real PDFium region renderer usable without writing Python. It creates an original-image asset and its source/transform manifest. This is a partial #7 foundation from main; it does not depend on the pending inspection CLI or parser adapters.

```bash
# From the repository, with services/parser/requirements.lock installed:
PYTHONPATH=services/parser python -m lab_parser.render_bundle source.pdf \
  --output-dir private-output/new-bundle --page-index 0 \
  --bbox 0.1 0.2 0.8 0.6 --dpi 150
```

Create the parent `private-output` beforehand. It must be trusted, exclusively controlled, and remain stable during the operation; this command is not a defense against a same-user adversary changing parent paths. Use an unused output directory name. The optional bbox defaults to the full page. It uses zero-based physical page indices and normalized top-left coordinates after rotation and visible CropBox clipping, matching the existing renderer. DPI defaults to 150 and is limited to 72–300. Rendering retains the renderer's 16 MP/4096-pixel maximum without silent downsampling or automatic retries. A 300-DPI call is an explicit separate operation, not automatic verification or an implicit upgrade policy.

The local input must be a regular file: a symlink in its final path component, directories and special files are rejected (ancestor directories remain trusted), and nonblocking opening avoids FIFO waits. `--max-bytes` can lower the 512 MiB ceiling. At most the configured limit plus one byte is read; an over-limit prefix is never parsed or assigned a complete source hash. Strict pypdf validation rejects encrypted PDFs (including empty-password encryption), out-of-range pages, over-2000-page documents, invalid selected-page geometry and warning-recovered input. The selected page's visible dimensions must agree with PDFium. This is not text/OCR extraction or verification that every page is valid.

Successful publication creates only:

- `asset.png`: deterministic PNG bytes from the existing renderer, mode 0600.
- `manifest.json`: internal `l0-render-bundle-1` JSON, mode 0600. It contains raw PDF SHA-256/size/page count, original page index and bbox, PNG SHA-256/size/dimensions/DPI, renderer version and the complete renderer transform. `status=unverified_l0` explicitly separates raw imagery from verified evidence or gold. No document ID, evidence ID, page label or semantic interpretation is invented.

The bundle directory is private (0700). Files are staged in the destination parent and flushed together with the stage directory. Linux `renameat2(RENAME_NOREPLACE)` publishes the completed directory atomically and refuses every existing destination, including empty directories and dangling symlinks. Unsupported platforms/filesystems fail closed; there is no overwrite fallback. Failed unpublished stages are removed when cleanup succeeds; `cleanup_failed` means a private stage remains for operator inspection. A crash before publication can leave a hidden `.l0-stage-*` directory requiring operator inspection; readers must use only the final named bundle. A crash during/after rename may leave the complete bundle present. The final parent-directory fsync is a durability barrier: if it fails, `publication_durability_unknown` means the published bundle remains present and must be inspected, not automatically deleted or replaced.

Exit 0 emits a small JSON success report with source/asset hashes. Exit 1 emits a fixed JSON failure code; the CLI suppresses Python and native parser stderr so raw PDF text and source paths are not printed. `--help` exits 0 with usage. API callers can use `lab_parser.render_bundle.create_bundle`; diagnostics capture and the CLI stderr suppression are process-global and require single-threaded use. Run this operator command in a dedicated process.

Byte/page/image limits do not contain malicious-parser CPU or RAM usage. This command does not provide subprocess/cgroup isolation, filesystem/network sandboxing, database/artifact registration, project authorization, trace collection, cancellation, resumable jobs or a backup protocol. Deployments must apply those controls separately; #5/#6/#7/#8 and the resource-isolation increment retain their integration gates. There is no human-quality, vision/provider, verified crop completeness, full numbered T/A, or release completion claim.

Actual synthetic-PDF tests in [test_render_bundle.py](../tests/parser/test_render_bundle.py) cover source/PNG identities, real rotations/CropBox/crop pixels, strict limits/inputs, encrypted/malformed/warning-recovered files, safe destination publication, cleanup and CLI diagnostics. These are deterministic local T01/T03/T05 and R01/R27 foundation subcases, not complete acceptance.
