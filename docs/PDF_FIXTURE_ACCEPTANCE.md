# PDFium fixture acceptance (#40, Refs #7)

This increment exercises the existing geometry/render library with the fixed
registry artifacts. `lab_parser.importer` is the prepared `issue-5-db` inspector,
extracted without its HTTP endpoint or DB changes. It computes the raw-byte hash,
size, strict pypdf validity, encryption status and page limit; repaired xrefs are
flagged with diagnostics. Positive integer limits are required. Its temporary
pypdf warning capture is serialized and limited to the inspecting thread.

## Reproduce

From the repository root, using Python 3.12+:

```bash
python3 -m pip install -r requirements-dev.txt -r services/parser/requirements.lock -r contracts/requirements.lock
LAB_PDF_FIXTURE_REPORT_DIR="$PWD/.lab-artifacts/pdf-fixture-evidence" \
LAB_TEST_REPORT_DIR="$PWD/.lab-artifacts/test-reports" \
scripts/check.sh --scope 'parser fixtures governance contracts'
```

No parser models, paid providers, database or credentials are needed for these
synthetic cases. The unchanged API capabilities continue to report both parser
adapters as not implemented. The inspector is an internal library only.

CI runs these cases inside the `parser` suite with real pypdfium2 5.14.0/PDFium;
there is no renderer mock in fixture acceptance. Changes to fixtures also select
`parser`, so future fixture changes cannot bypass this acceptance. The CI run
retains `test-reports` plus `pdf-fixture-evidence` (90 days). The latter contains
synthetic full-page/crop/negative-control PNGs and `evidence.json` with exact
commit, working diff hash, dependency/PDFium versions, UTC times, executor/run,
profile, fixture and expected-file hashes, per-case expected/actual/status,
transforms, image SHA-256/size and remaining gates. The PR records the exact head
and CI run; neither a local uncommitted run nor a green unrelated head is CI proof.
Class cleanup writes failed evidence even when registry loading fails during setup,
so repeating a run cannot leave an earlier passed report after that failure.

## Fixed oracles and scope

The registry hashes are verified before any acceptance input is used. Golden
coordinates come from the committed expected JSON, and the PDF rectangles are
literal `(72,600,272,660)` and `(340,300,540,360)`. Tests never regenerate golden
coordinates with the production transform. The existing registry content and
its partial/human-signoff status are unchanged.

| Test | Actual subcases | Remaining integration under #7 / release |
| --- | --- | --- |
| T01 / R01 | F01 raw-byte SHA, strict inspection, page counts 1/19/20/21/500/600; all physical indices; independent PDFium first/last page text identity | Upload/download, immutable document/version uniqueness, sufficient-content variants |
| T02 / R01 | All fixed F05 empty/fake/truncated/repair outcomes and hash/size; injection text accepted/rendered as data | HTTP 400/415/422, content type, proving no LLM dispatch in the future upload path |
| T03 / R01 | RC4 encryption rejected without password guessing; 2001 pages rejected at default 2000; exact byte/page boundary accepted and one over rejected using small configured limits | Actual 512 MiB resource profile, AES fixture |
| T04 / R01 | Same raw bytes have the same hash; different valid bytes have different hashes | Artifact reuse and new immutable version persistence |
| T05 / R02 / R27 | All 6 F02 pages, 12 markers, rotations 0/90/180/270, both offset CropBoxes, physical index vs printed label; real L0 full/crop pixels, fixed geometry and negative transforms | Browser preview/highlight, persisted asset lineage; missing scans/CJK/reading-order content |

T05 uses fixed display sizes `(612,792)`, `(792,612)`, `(612,792)`,
`(792,612)`, `(540,648)`, `(648,540)` points. Full pages at 72 and 150 DPI must
match those sizes within one pixel. An independent flood fill locates both gray
marker extents in the **production L0 PNG**, with maximum coordinate error
≤0.005. The production forward transform must match golden within 0.000001;
the inverse is compared with literal PDF rectangles within the T05 tolerance.
Golden marker crops must have >80% gray pixels (217 ±6 per RGB channel), correct
pixel dimensions/metadata/hash, and byte-identical repeat renders.

Wrong rotation (+90 degrees) on every marker and ignoring the CropBox on offset
pages must exceed the 0.005 localization tolerance. Their crops/hashes/pixel
ratios are retained as negative controls. A misplaced crop can still contain a
large gray fill, so gray ratio alone is deliberately insufficient to pass the
localization oracle. Unique `MARK-<physical index>-L/R` text is read independently
by PDFium to catch page confusion; printed labels are never used as indices.

These are deterministic integration/unit subcases, not complete T01–T05 or A19
acceptance. Upload, PostgreSQL/artifact transactions, workflow/batch recovery,
OCR, Unstructured/MinerU, vision, human signoff and the full T01–T104 release gates
remain open. #40 can close only after its scoped CI evidence passes; #7 stays open.
