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
    # /PageLabels; those are not printed page labels, so they are not used.
    # A malformed label tree raises instead of being silently dropped.
    label_list: list[Any] = []
    if "/PageLabels" in reader.trailer["/Root"]:
        label_list = list(reader.page_labels)

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
