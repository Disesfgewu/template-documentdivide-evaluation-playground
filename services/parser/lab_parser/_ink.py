"""Render-based ink map of a page, for the cross-check of evidence regions (gap G8 (b), Refs #7).

Loaded by path inside the isolation child (``python -I``), so it imports nothing from the package.
Each page is rendered once by PDFium at 72 dpi in grayscale (this applies /Rotate and /CropBox, the same
frame as the L0 display frame) and reduced to a GRID x GRID occupancy map: a cell has ink when any of its
pixels is not white. The parent keeps an evidence region only if a cell under it has ink, which catches
text that is rendered but invisible (white on white), outside the page or clipped away. It does NOT catch
hidden text drawn over other ink (a region that also contains visible marks, text covered by an image).
"""
from __future__ import annotations

GRID = 128
MAX_PIXELS = 4_000_000       # per page at 72 dpi (about 2000 x 2000 pt); larger pages are refused
INK_BELOW = 245              # luminance 0..255; antialiased black text is well below this, white is 255
MAX_PAGE_SIDE_PT = 5000


def page_ink(page) -> list[str] | None:
    """GRID hex strings (one per grid row, GRID/4 hex digits each, bit set = ink), or ``None`` when the
    page cannot be rendered within the caps (the caller fails closed)."""
    try:
        width_pt, height_pt = page.get_size()
        if not (0 < width_pt <= MAX_PAGE_SIDE_PT and 0 < height_pt <= MAX_PAGE_SIDE_PT):
            return None
        if width_pt * height_pt > MAX_PIXELS:
            return None
        bitmap = page.render(scale=1.0, grayscale=True)
        try:
            width, height, stride = int(bitmap.width), int(bitmap.height), int(bitmap.stride)
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                return None
            buffer = bytes(bitmap.buffer)
        finally:
            bitmap.close()
        if len(buffer) < stride * (height - 1) + width:
            return None
        edges = [c * width // GRID for c in range(GRID + 1)]
        rows = [0] * GRID
        for y in range(height):
            row = buffer[y * stride:y * stride + width]
            gy = y * GRID // height
            bits = rows[gy]
            for c in range(GRID):
                lo, hi = edges[c], max(edges[c + 1], edges[c] + 1)
                if hi > width:
                    hi = width
                if lo < hi and min(row[lo:hi]) < INK_BELOW:
                    bits |= 1 << c
            rows[gy] = bits
        return [format(bits, "0%dx" % (GRID // 4)) for bits in rows]
    except Exception:  # noqa: BLE001 - anything unexpected is not proof that the page has ink
        return None


def document_ink(path: str) -> list[list[str] | None]:
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
