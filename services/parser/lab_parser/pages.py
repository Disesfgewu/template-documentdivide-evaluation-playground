"""Page-by-page PDF geometry extraction and normalization.

This module provides data structures and functions to parse PDF byte streams
using pypdf, extracting page labels, rotation, MediaBox, CropBox, visible box,
and display width/height dimensions in points.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject


@dataclass(frozen=True)
class PageInfo:
    """Extracted geometric and layout information for a single PDF page.

    Attributes:
        page_index: 0-based physical page index.
        page_label: Label from the document's /PageLabels, or None when it has none.
        rotation: Normalized page rotation in degrees (0, 90, 180, or 270).
        mediabox: MediaBox boundary as (x0, y0, x1, y1) in points.
        cropbox: CropBox boundary as (x0, y0, x1, y1) in points.
        visible_box: Intersection of CropBox and MediaBox as (x0, y0, x1, y1) in points.
        display_width_pt: Rendered page display width in points after rotation.
        display_height_pt: Rendered page display height in points after rotation.
    """

    page_index: int
    page_label: str | None
    rotation: int
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    visible_box: tuple[float, float, float, float]
    display_width_pt: float
    display_height_pt: float


def _normalize_rotation(rotation: int) -> int:
    """Normalize a rotation angle in degrees to {0, 90, 180, 270}.

    Args:
        rotation: Page rotation angle in degrees (can be negative or >= 360).

    Returns:
        Normalized rotation in {0, 90, 180, 270}.

    Raises:
        ValueError: If rotation is not a multiple of 90 degrees.
    """
    if rotation % 90 != 0:
        raise ValueError(f"Rotation {rotation} is not a multiple of 90 degrees")
    return int(rotation) % 360


def _parse_box(rect: Any) -> tuple[float, float, float, float]:
    """Convert a pypdf RectangleObject or coordinate sequence to (x0, y0, x1, y1).

    Args:
        rect: A pypdf RectangleObject or 4-element sequence of coordinate numbers.

    Returns:
        Tuple of (left, bottom, right, top) coordinates as floats where x0 <= x1
        and y0 <= y1.

    Raises:
        ValueError: If rect does not contain 4 coordinate numbers.
    """
    if (
        hasattr(rect, "left")
        and hasattr(rect, "bottom")
        and hasattr(rect, "right")
        and hasattr(rect, "top")
    ):
        x0 = float(rect.left)
        y0 = float(rect.bottom)
        x1 = float(rect.right)
        y1 = float(rect.top)
    else:
        coords = [float(v) for v in rect]
        if len(coords) != 4:
            raise ValueError(f"Rectangle must have 4 values, got {len(coords)}")
        x0 = min(coords[0], coords[2])
        y0 = min(coords[1], coords[3])
        x1 = max(coords[0], coords[2])
        y1 = max(coords[1], coords[3])
    return (x0, y0, x1, y1)


def _intersect_boxes(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Calculate the intersection of two bounding boxes.

    Args:
        box_a: First boundary as (x0, y0, x1, y1).
        box_b: Second boundary as (x0, y0, x1, y1).

    Returns:
        Intersection boundary as (x0, y0, x1, y1).

    Raises:
        ValueError: If box_a and box_b do not intersect or have non-positive area.
    """
    x0 = max(box_a[0], box_b[0])
    y0 = max(box_a[1], box_b[1])
    x1 = min(box_a[2], box_b[2])
    y1 = min(box_a[3], box_b[3])

    if x0 >= x1 or y0 >= y1:
        raise ValueError(
            f"CropBox and MediaBox have no valid intersection: {box_a} and {box_b}"
        )
    return (x0, y0, x1, y1)


class InvalidPageLabels(ValueError):
    """The document's /PageLabels number tree is malformed (PDF 32000 7.9.7,
    12.4.2). Raised instead of guessing a label."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid_page_labels: {detail}")


_MAX_TREE_DEPTH = 32
# A printed page label is short. The bound is checked before any label text
# is built, so an adversarial /St (e.g. /A with 26000001) cannot allocate
# huge strings or loop in roman formatting (PM review on #36).
MAX_LABEL_CHARS = 64
MAX_LABEL_NUMBER = 10**9


def _label_ranges(node: Any, depth: int, out: list[tuple[int, Any]]) -> None:
    node = node.get_object() if hasattr(node, "get_object") else node
    if not isinstance(node, DictionaryObject):
        raise InvalidPageLabels("tree node is not a dictionary")
    if depth > _MAX_TREE_DEPTH:
        raise InvalidPageLabels("tree too deep")
    has_nums, has_kids = "/Nums" in node, "/Kids" in node
    if has_nums == has_kids:
        raise InvalidPageLabels("node needs exactly one of /Nums or /Kids")
    if has_kids:
        kids = node["/Kids"].get_object()
        if not isinstance(kids, ArrayObject) or not kids:
            raise InvalidPageLabels("/Kids is not a non-empty array")
        for kid in kids:
            _label_ranges(kid, depth + 1, out)
        return
    nums = node["/Nums"].get_object()
    if not isinstance(nums, ArrayObject) or not nums or len(nums) % 2:
        raise InvalidPageLabels("/Nums is not a non-empty array of key/value pairs")
    for k in range(0, len(nums), 2):
        key, value = nums[k], nums[k + 1].get_object()
        if isinstance(key, bool) or not isinstance(key, int) or key < 0:
            raise InvalidPageLabels(f"page index key {key!r} is not a non-negative integer")
        if not isinstance(value, DictionaryObject):
            raise InvalidPageLabels(f"label for page {key} is not a dictionary")
        out.append((int(key), value))


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                       (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + sym, n - value
    return out


def _letters(n: int) -> str:
    # PDF 12.4.2: a..z, then aa..zz, aaa..zzz.
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _format_label(spec: Any, number: int) -> str:
    style = spec.get("/S")
    prefix = spec.get("/P", "")
    if style is not None and style not in {"/D", "/R", "/r", "/A", "/a"}:
        raise InvalidPageLabels(f"unknown numbering style {style!r}")
    if not isinstance(prefix, str):
        raise InvalidPageLabels("/P is not a text string")
    if len(prefix) > MAX_LABEL_CHARS:
        raise InvalidPageLabels(f"/P longer than {MAX_LABEL_CHARS} characters")
    if style is None:
        return str(prefix)
    if number > MAX_LABEL_NUMBER:
        raise InvalidPageLabels(f"page number {number} exceeds {MAX_LABEL_NUMBER}")
    # Exact rendered length, computed without rendering.
    if style == "/D":
        length = len(str(number))
    elif style in {"/A", "/a"}:
        length = (number - 1) // 26 + 1
    else:  # roman: one "m" per thousand plus the exact numeral of the rest (< 1000)
        length = number // 1000 + len(_roman(number % 1000))
    if len(prefix) + length > MAX_LABEL_CHARS:
        raise InvalidPageLabels(f"label for page number {number} would exceed {MAX_LABEL_CHARS} characters")
    if style == "/D":
        body = str(number)
    elif style in {"/R", "/r"}:
        body = _roman(number)
    else:
        body = _letters(number)
    return str(prefix) + (body.upper() if style in {"/R", "/A"} else body)


def page_labels(reader: PdfReader) -> list[str | None]:
    """Printed labels from the document's own /PageLabels, one per page, or
    all None when it has none. Raises InvalidPageLabels on a malformed tree,
    never falls back to invented numbers."""
    n = len(reader.pages)
    root = reader.trailer["/Root"].get_object()
    if "/PageLabels" not in root:
        return [None] * n
    ranges: list[tuple[int, Any]] = []
    _label_ranges(root["/PageLabels"], 0, ranges)
    keys = [k for k, _ in ranges]
    if keys != sorted(set(keys)):
        raise InvalidPageLabels("page index keys are not strictly increasing")
    if keys[0] != 0:
        raise InvalidPageLabels("the tree has no entry for page index 0")
    labels: list[str | None] = []
    for index in range(n):
        start, spec = [r for r in ranges if r[0] <= index][-1]
        first = spec.get("/St", 1)
        if isinstance(first, bool) or not isinstance(first, int) or first < 1:
            raise InvalidPageLabels(f"/St {first!r} is not a positive integer")
        if first > MAX_LABEL_NUMBER:
            raise InvalidPageLabels(f"/St {first} exceeds {MAX_LABEL_NUMBER}")
        labels.append(_format_label(spec, int(first) + index - start))
    return labels


def extract_pages(data: bytes) -> list[PageInfo]:
    """Extract per-page geometry, rotation, boxes, and display dimensions from PDF bytes.

    Reads a PDF document from raw bytes, extracts each page's index, printed page label
    (if available), rotation (normalized to 0/90/180/270), MediaBox, CropBox, visible box
    (intersection of CropBox and MediaBox), and effective display width and height in points.

    Args:
        data: Raw bytes of the PDF document.

    Returns:
        List of PageInfo objects corresponding to each page in document order.

    Raises:
        pypdf.errors.PdfReadError: If the PDF byte stream is malformed or invalid.
        ValueError: If page rotation is not a multiple of 90 degrees,
            or if MediaBox and CropBox do not intersect.
    """
    reader = PdfReader(io.BytesIO(data), strict=True)

    # pypdf invents decimal labels ("1", "2", ...) when the document has no
    # /PageLabels or when the tree is malformed; those are not printed page
    # labels. The tree is parsed strictly here and a malformed one raises.
    label_list = page_labels(reader)

    pages_info: list[PageInfo] = []
    for idx, page in enumerate(reader.pages):
        page_label: str | None = None
        if idx < len(label_list):
            raw_val = label_list[idx]
            if raw_val is not None:
                page_label = str(raw_val)

        rot_val = getattr(page, "rotation", 0)
        if rot_val is None:
            rot_val = 0
        rotation = _normalize_rotation(int(rot_val))

        mediabox = _parse_box(page.mediabox)
        cropbox = _parse_box(page.cropbox)

        visible_box = _intersect_boxes(mediabox, cropbox)

        unrotated_width = visible_box[2] - visible_box[0]
        unrotated_height = visible_box[3] - visible_box[1]

        if rotation in (90, 270):
            display_width_pt = float(unrotated_height)
            display_height_pt = float(unrotated_width)
        else:
            display_width_pt = float(unrotated_width)
            display_height_pt = float(unrotated_height)

        pages_info.append(
            PageInfo(
                page_index=idx,
                page_label=page_label,
                rotation=rotation,
                mediabox=mediabox,
                cropbox=cropbox,
                visible_box=visible_box,
                display_width_pt=display_width_pt,
                display_height_pt=display_height_pt,
            )
        )

    return pages_info
