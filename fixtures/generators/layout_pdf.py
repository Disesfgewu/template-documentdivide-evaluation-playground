"""Deterministic layout fixture (F02 subset): page rotation 0/90/180/270,
CropBox smaller than MediaBox, printed page labels (roman then arabic) and a
two-column layout. Each page carries two labelled marker boxes at known
user-space positions; ``expected()`` gives their bbox in SPEC 4.1 space
(top-left origin, after /Rotate, normalized to the visible CropBox).

Not covered here (listed as missing in the registry): scanned/OCR pages,
CJK text (needs an embedded font) and mixed scanned+text pages.
Usage: python -m fixtures.generators.layout_pdf <out.pdf>
"""
import hashlib
import sys

MEDIA = (0, 0, 612, 792)
# (rotate, cropbox) per page
PAGES = [
    (0, MEDIA),
    (90, MEDIA),
    (180, MEDIA),
    (270, MEDIA),
    (0, (36, 72, 576, 720)),
    (90, (36, 72, 576, 720)),
]
# Marker boxes in PDF user space (x, y, w, h): left and right column.
MARKERS = [("L", (72, 600, 200, 60)), ("R", (340, 300, 200, 60))]
# /PageLabels: pages 0-1 roman (i, ii), then arabic from 1.
LABELS = ["i", "ii", "1", "2", "3", "4"]


def _normalize(box, crop, rotate):
    """Reference transform, derived by hand per rotation (see module doc)."""
    x, y, w, h = box
    cx0, cy0, cx1, cy1 = crop
    cw, ch = cx1 - cx0, cy1 - cy0
    # Unrotated, top-left origin, normalized to CropBox.
    u0, u1 = (x - cx0) / cw, (x + w - cx0) / cw
    v0, v1 = (cy1 - (y + h)) / ch, (cy1 - y) / ch
    # /Rotate turns the displayed page clockwise.
    corners = [(u0, v0), (u1, v1)]
    if rotate == 0:
        pts = corners
    elif rotate == 90:
        pts = [(1 - v, u) for u, v in corners]
    elif rotate == 180:
        pts = [(1 - u, 1 - v) for u, v in corners]
    elif rotate == 270:
        pts = [(v, 1 - u) for u, v in corners]
    else:
        raise ValueError(rotate)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]


def expected() -> dict:
    pages = []
    for i, (rot, crop) in enumerate(PAGES):
        pages.append({
            "page_index": i, "page_label": LABELS[i], "rotate": rot,
            "mediabox": list(MEDIA), "cropbox": list(crop),
            "markers": [{"text": f"MARK-{i}-{name}", "bbox": _normalize(box, crop, rot)}
                        for name, box in MARKERS],
        })
    return {"pages": pages}


def build() -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for i, (rot, crop) in enumerate(PAGES):
        ops = []
        for name, (x, y, w, h) in MARKERS:
            ops.append(f"0.85 g {x} {y} {w} {h} re f 0 g")
            ops.append(f"BT /F1 10 Tf {x + 6} {y + h / 2 - 3:g} Td (MARK-{i}-{name}) Tj ET")
        ops.append(f"BT /F1 9 Tf 72 40 Td (page {i + 1} rotate {rot}) Tj ET")
        stream = "\n".join(ops).encode("ascii")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [%d %d %d %d] /CropBox [%d %d %d %d] /Rotate %d "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_obj, *MEDIA, *crop, rot, font, content)))
    objs[catalog - 1] = (b"<< /Type /Catalog /Pages %d 0 R /PageLabels << /Nums "
                         b"[0 << /S /r >> 2 << /S /D /St 1 >>] >> >>" % pages_obj)
    objs[pages_obj - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                           + b"] /Count %d >>" % len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    data = build()
    with open(argv[0], "wb") as f:
        f.write(data)
    print(hashlib.sha256(data).hexdigest(), argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
