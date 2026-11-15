"""Map coordinates from pdfminer/Unstructured layout space into the L0 display frame (Refs #7).

Measured on CI with real unstructured 0.27.36 (e2e ``bbox_measurement``): for a page with ``/Rotate``
the upstream coordinates are already in the *rotated* MediaBox display space (layout size is the
rotated MediaBox size), but ``/CropBox`` is ignored, so the page content is positioned relative to
the MediaBox. The L0 display frame is the rotated visible box (CropBox intersected with MediaBox).
Therefore a point converts by subtracting the origin of the rotated visible box.

This module is pure geometry. Whether a conversion is *trusted* is decided elsewhere
(``unstructured_runner``): only rotations whose text units stay intact are used, and the result is
validated against PDFium's rendered ink box in the CI-only e2e test.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

Box = Sequence[float]


@dataclass(frozen=True)
class FrameTransform:
    layout_width: float    # size of the upstream layout space (rotated MediaBox)
    layout_height: float
    origin_x: float        # rotated visible box origin inside the layout space
    origin_y: float
    width: float           # display size (rotated visible box)
    height: float


def _finite_box(box: Box) -> tuple[float, float, float, float]:
    if len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
        raise ValueError("invalid_box")
    x0, y0, x1, y1 = (float(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("invalid_box")
    return x0, y0, x1, y1


def display_transform(mediabox: Box, cropbox: Box, rotation: int) -> FrameTransform:
    """The transform for one page. ``rotation`` is /Rotate normalized to 0, 90, 180 or 270."""
    if rotation not in (0, 90, 180, 270):
        raise ValueError("invalid_rotation")
    mx0, my0, mx1, my1 = _finite_box(mediabox)
    cx0, cy0, cx1, cy1 = _finite_box(cropbox)
    vx0, vy0, vx1, vy1 = max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1)
    if vx1 <= vx0 or vy1 <= vy0:
        raise ValueError("empty_visible_box")
    w, h = mx1 - mx0, my1 - my0
    # Visible box in unrotated top-left coordinates, relative to the MediaBox.
    left, right, top, bottom = vx0 - mx0, vx1 - mx0, my1 - vy1, my1 - vy0
    corners = [(left, top), (right, top), (left, bottom), (right, bottom)]
    if rotation == 0:
        moved, lw, lh = corners, w, h
    elif rotation == 90:
        moved, lw, lh = [(h - y, x) for x, y in corners], h, w
    elif rotation == 180:
        moved, lw, lh = [(w - x, h - y) for x, y in corners], w, h
    else:
        moved, lw, lh = [(y, w - x) for x, y in corners], h, w
    xs, ys = [p[0] for p in moved], [p[1] for p in moved]
    return FrameTransform(lw, lh, min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
