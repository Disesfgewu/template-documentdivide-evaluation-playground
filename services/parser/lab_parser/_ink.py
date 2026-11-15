"""Render-based ink map of a page, for the cross-check of evidence regions (gap G8 (b), Refs #7).

Loaded by path inside the isolation child (``python -I``), so it imports nothing from the package.
Each page is rendered once by PDFium at 72 dpi in grayscale, with form fields and annotations OFF (the same
as ``render.py``; PDFium applies /Rotate and /CropBox, so this is the L0 display frame), and reduced to a
GRID x GRID occupancy map. A cell counts as inked when at least ``MIN_CELL_PIXELS`` of its pixels are not
white (luminance below ``INK_BELOW``), so one anti-aliased pixel is not ink. The parent keeps an evidence
region only if enough of the cells under it are inked (``MIN_INK_FRACTION`` in the runner). The map also
carries the rendered page size, which the parent compares with the attested frame.

NOT caught: hidden text drawn under or among other ink (a region that also contains visible marks, text
covered by an image or a filled shape). Near-white text (luminance >= INK_BELOW) counts as no ink.
"""
from __future__ import annotations

GRID = 128
MAX_PIXELS = 4_000_000       # per page at 72 dpi (about 2000 x 2000 pt); larger pages are refused
INK_BELOW = 245              # luminance 0..255; antialiased black text is well below this, white is 255
MIN_CELL_PIXELS = 3          # a cell is inked only with at least this many non-white pixels
MAX_PAGE_SIDE_PT = 5000


def grid_from_gray(buffer, width: int, height: int, stride: int) -> list[str]:
    """GRID hex strings (one per grid row, GRID/4 hex digits each, bit c set = column c inked)."""
    table = bytes(1 if value < INK_BELOW else 0 for value in range(256))
    edges = [c * width // GRID for c in range(GRID + 1)]
    counts = [[0] * GRID for _ in range(GRID)]
    for y in range(height):
        row = bytes(buffer[y * stride:y * stride + width]).translate(table)
        if not row.count(1):
            continue
        cells = counts[y * GRID // height]
        for c in range(GRID):
            lo, hi = edges[c], edges[c + 1]
            if lo < hi:
                cells[c] += sum(row[lo:hi])
    rows = []
    for cells in counts:
        bits = 0
        for c in range(GRID):
            if cells[c] >= MIN_CELL_PIXELS:
                bits |= 1 << c
        rows.append(format(bits, "0%dx" % (GRID // 4)))
    return rows


def page_ink(page) -> dict | None:
    """``{"size": [width_pt, height_pt], "rows": [...]}`` or ``None`` when the page cannot be rendered within
    the caps (the caller fails closed)."""
    try:
        width_pt, height_pt = page.get_size()
        if not (0 < width_pt <= MAX_PAGE_SIDE_PT and 0 < height_pt <= MAX_PAGE_SIDE_PT):
            return None
        if width_pt * height_pt > MAX_PIXELS:
            return None
        bitmap = page.render(scale=1.0, grayscale=True, may_draw_forms=False, draw_annots=False)
        try:
            width, height, stride = int(bitmap.width), int(bitmap.height), int(bitmap.stride)
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                return None
            buffer = bytes(bitmap.buffer)
        finally:
            bitmap.close()
        if len(buffer) < stride * (height - 1) + width:
            return None
        return {"size": [float(width_pt), float(height_pt)], "rows": grid_from_gray(buffer, width, height, stride)}
    except Exception:  # noqa: BLE001 - anything unexpected is not proof that the page has ink
        return None


def document_ink(path: str) -> list:
    """One ink map (or ``None``) per page of the PDF at ``path``."""
    import pypdfium2

    document = pypdfium2.PdfDocument(path)
    try:
        maps = []
        for index in range(len(document)):
            page = document[index]
            try:
                maps.append(page_ink(page))
            finally:
                page.close()
        return maps
    finally:
        document.close()
