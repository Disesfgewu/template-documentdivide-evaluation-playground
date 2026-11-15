# Bounded crop escalation planning (Refs #7)

`lab_parser.escalation.plan_next` plans renders without performing them or claiming parser/vision quality. `dpi-escalation-2` replaces version1's rounded size estimates with the pinned PDFium canvas/crop quantization shared by `render_region` preflight. Callers supply the visible, rotated page size from PDFium. Actual bitmap dimensions are independently checked at render time; upgrading PDFium requires these integration regressions to run again.

An empty quantized crop requires review; it is never replaced by a fabricated pixel. Over-limit crops use overlapping tiles without downscaling, each with its own normalized box, actual dimensions and pixel origin in the whole crop. The budget includes all tile pixels. Positive integer actual history counts are preserved; negative, boolean, fractional or nonfinite counts and invalid/out-of-policy DPI/geometry/profile/history fail closed. Excess real usage goes to the budget gate. Oversized numeric geometry is rejected with fixed input codes.

The default150→300DPI escalation, finite attempt/tile caps and pixel budgets remain. The planner does not persist artifacts, hashes, actual usage, parser results or checkpoints; execution and #5/#6/#8 integration are still required. This increment does not pass T104 or parent#7.

Evidence: synthetic PDFs rendered with real pinned pypdfium2, fractional full/crop boundaries,4096/4097 side limit and rotated nonzero CropBox cases. Colored stripe fixtures compare each tile's actual RGB bytes to the whole-crop slice at its declared origin for0/90/180/270 rotations. No model/provider/human gold or production performance evidence is claimed.
