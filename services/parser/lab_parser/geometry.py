"""PDF user-space (pt, bottom-left origin) <-> LAB SourceRegion space ([0,1], top-left origin)."""

from __future__ import annotations

import math

Box = tuple[float, float, float, float]

_VALID_ROTATES = (0, 90, 180, 270)


def _normalize_rotate(rotate: int) -> int:
    if isinstance(rotate, bool) or not isinstance(rotate, int):
        raise ValueError(f"invalid_rotate: {rotate!r}")
    r = rotate % 360
    if r not in _VALID_ROTATES:
        raise ValueError(f"invalid_rotate: {rotate}")
    return r


def _validate_cropbox(cropbox: Box) -> tuple[float, float, float, float, float, float]:
    if len(cropbox) != 4:
        raise ValueError("invalid_cropbox")
    cx0, cy0, cx1, cy1 = (
        float(cropbox[0]),
        float(cropbox[1]),
        float(cropbox[2]),
        float(cropbox[3]),
    )
    if not (
        math.isfinite(cx0)
        and math.isfinite(cy0)
        and math.isfinite(cx1)
        and math.isfinite(cy1)
    ):
        raise ValueError("invalid_cropbox")
    cw = cx1 - cx0
    ch = cy1 - cy0
    if cw <= 0.0 or ch <= 0.0:
        raise ValueError("zero_area_cropbox")
    return cx0, cy0, cx1, cy1, cw, ch


def _validate_box(box: Box, label: str = "invalid_box") -> tuple[float, float, float, float]:
    if len(box) != 4:
        raise ValueError(label)
    x0, y0, x1, y1 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
    if not (
        math.isfinite(x0)
        and math.isfinite(y0)
        and math.isfinite(x1)
        and math.isfinite(y1)
    ):
        raise ValueError(label)
    if x0 >= x1 or y0 >= y1:
        raise ValueError(label)
    return x0, y0, x1, y1


def _rotate_uv(u: float, v: float, rotate: int) -> tuple[float, float]:
    if rotate == 0:
        return u, v
    if rotate == 90:
        return 1.0 - v, u
    if rotate == 180:
        return 1.0 - u, 1.0 - v
    # rotate == 270
    return v, 1.0 - u


def _unrotate_uv(rx: float, ry: float, rotate: int) -> tuple[float, float]:
    if rotate == 0:
        return rx, ry
    if rotate == 90:
        return ry, 1.0 - rx
    if rotate == 180:
        return 1.0 - rx, 1.0 - ry
    # rotate == 270
    return 1.0 - ry, rx


def to_region_space(
    box: tuple[float, float, float, float],
    cropbox: tuple[float, float, float, float],
    rotate: int,
) -> tuple[float, float, float, float]:
    """Convert a PDF user-space box (bottom-left origin, pt) to LAB SourceRegion bbox ([0,1], top-left origin)."""
    r = _normalize_rotate(rotate)
    cx0, cy0, cx1, cy1, cw, ch = _validate_cropbox(cropbox)
    x0, y0, x1, y1 = _validate_box(box, "invalid_box")

    ix0 = max(x0, cx0)
    iy0 = max(y0, cy0)
    ix1 = min(x1, cx1)
    iy1 = min(y1, cy1)
    if ix0 >= ix1 or iy0 >= iy1:
        raise ValueError("outside_cropbox")

    u0 = (ix0 - cx0) / cw
    u1 = (ix1 - cx0) / cw
    v0 = (cy1 - iy0) / ch
    v1 = (cy1 - iy1) / ch

    p0 = _rotate_uv(u0, v0, r)
    p1 = _rotate_uv(u1, v1, r)

    rx0 = max(0.0, min(p0[0], p1[0]))
    ry0 = max(0.0, min(p0[1], p1[1]))
    rx1 = min(1.0, max(p0[0], p1[0]))
    ry1 = min(1.0, max(p0[1], p1[1]))

    if rx0 >= rx1 or ry0 >= ry1:
        raise ValueError("outside_cropbox")

    return (rx0, ry0, rx1, ry1)


def to_user_space(
    region: tuple[float, float, float, float],
    cropbox: tuple[float, float, float, float],
    rotate: int,
) -> tuple[float, float, float, float]:
    """Convert a LAB SourceRegion bbox ([0,1], top-left origin) back to PDF user-space (bottom-left origin, pt)."""
    r = _normalize_rotate(rotate)
    cx0, _cy0, _cx1, cy1, cw, ch = _validate_cropbox(cropbox)
    rx0, ry0, rx1, ry1 = _validate_box(region, "invalid_region")
    if rx0 < 0.0 or ry0 < 0.0 or rx1 > 1.0 or ry1 > 1.0:
        raise ValueError("invalid_region")

    u_a, v_a = _unrotate_uv(rx0, ry0, r)
    u_b, v_b = _unrotate_uv(rx1, ry1, r)

    xa = cx0 + u_a * cw
    xb = cx0 + u_b * cw
    ya = cy1 - v_a * ch
    yb = cy1 - v_b * ch

    x0 = min(xa, xb)
    y0 = min(ya, yb)
    x1 = max(xa, xb)
    y1 = max(ya, yb)

    return (x0, y0, x1, y1)
