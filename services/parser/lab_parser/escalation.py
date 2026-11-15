"""Limited re-render escalation for a weak visual crop (SPEC 19.3 item 8).

Pure planning: given the attempts already made for one asset and what was wrong
with the last one, decide the next render, or stop and ask for review. The
SPEC fixes the shape: the first render is 150 DPI; at most one escalation to
300 DPI; other remedies are a wider box or another profile; every attempt keeps
its own real pixels, crop and hash; going over the pixel or cost limit means
review, never an unbounded retry; the crop is never silently downscaled, an
over-limit crop is cut into tiles that each carry the transform back to the crop.

Boxes are normalized page coordinates ``(x0, y0, x1, y1)`` in ``[0, 1]`` with
the origin top-left, and ``page_size_pt`` is the visible page size in points, as
in ``render.render_region``. Rasterization dimensions follow PDFium's exact crop
quantization semantics (scale = dpi / 72.0; canvas ceil-quantized; crop edges
ceil-quantized in canvas units; dimensions = src_dim - crop_start - crop_end).
Planned sizes match the exact pixel dimensions that ``render_region`` produces
for both full-page and crop renders.

The defaults are adjustable design values, not tool guarantees (SPEC): changing
one needs a new ``POLICY_VERSION`` and re-testing. A policy may lower the pixel
limits (an upstream limit smaller than ours wins) but never raise them.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence

from .render import MAX_DPI, MAX_PIXELS, MAX_SIDE_PX, MIN_DPI, raster_size_px

__all__ = [
    "Attempt",
    "EscalationError",
    "EscalationPolicy",
    "Plan",
    "Tile",
    "WEAKNESSES",
    "plan_next",
]

POLICY_VERSION = "dpi-escalation-2"
WEAKNESSES = ("none", "too_small", "blurry", "missing_labels")
Box = tuple[float, float, float, float]


class EscalationError(ValueError):
    """Invalid input; the message is a fixed reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EscalationPolicy:
    first_dpi: int = 150
    escalated_dpi: int = 300
    max_attempts: int = 3            # the first render plus at most two remedies
    expand_margin: float = 0.10      # each side, as a fraction of the box size
    alternate_profiles: tuple[str, ...] = ()
    max_pixels: int = MAX_PIXELS     # per rendered image (16 MP); may only be lowered
    max_side_px: int = MAX_SIDE_PX   # per rendered image (4096 px); may only be lowered
    tile_overlap_px: int = 64        # shared border so a label on a seam is whole in one tile
    max_tiles: int = 16
    pixel_budget: int = 64_000_000   # all attempts together; the cost bound

    def __post_init__(self) -> None:
        for name in ("first_dpi", "escalated_dpi", "max_attempts", "max_pixels", "max_side_px",
                     "max_tiles", "pixel_budget", "tile_overlap_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise EscalationError("invalid_policy")
        if not (MIN_DPI <= self.first_dpi < self.escalated_dpi <= MAX_DPI):
            raise EscalationError("invalid_policy")
        if not 1 <= self.max_attempts <= 6 or not 1 <= self.max_tiles <= 64:
            raise EscalationError("invalid_policy")
        if not 1 <= self.max_pixels <= MAX_PIXELS or not 1 <= self.max_side_px <= MAX_SIDE_PX:
            raise EscalationError("invalid_policy")  # limits may be lowered, not raised
        if self.tile_overlap_px < 0 or 2 * self.tile_overlap_px >= self.max_side_px:
            raise EscalationError("invalid_policy")
        if self.pixel_budget < self.max_pixels:
            raise EscalationError("invalid_policy")
        if isinstance(self.expand_margin, bool) or not isinstance(self.expand_margin, (int, float)) \
                or not math.isfinite(self.expand_margin) or not 0 < self.expand_margin <= 0.5:
            raise EscalationError("invalid_policy")
        if not all(isinstance(p, str) and 1 <= len(p) <= 64 for p in self.alternate_profiles) \
                or len(set(self.alternate_profiles)) != len(self.alternate_profiles):
            raise EscalationError("invalid_policy")


@dataclass(frozen=True)
class Attempt:
    """One finished render of the asset: what was asked for and what came out."""

    dpi: int
    box: Box
    profile: str
    pixels: int          # actual pixels rendered (all tiles together)


@dataclass(frozen=True)
class Tile:
    """A part of an over-limit crop. ``origin_px`` is the tile's top-left in the pixel
    grid of the whole crop at this DPI, so a position in the tile maps back exactly."""

    box: Box
    width_px: int
    height_px: int
    origin_px: tuple[int, int]


@dataclass(frozen=True)
class Plan:
    action: str                       # render | accept | needs_review
    attempt_no: Optional[int]
    dpi: Optional[int]
    box: Optional[Box]
    profile: Optional[str]
    width_px: Optional[int]
    height_px: Optional[int]
    tiles: tuple[Tile, ...]
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION


def _review(*reasons: str) -> Plan:
    return Plan("needs_review", None, None, None, None, None, None, (), tuple(reasons))


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _check_box(box: object) -> Box:
    if not isinstance(box, tuple) or len(box) != 4:
        raise EscalationError("invalid_box")
    for v in box:
        if not _finite_number(v):
            raise EscalationError("invalid_box")
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise EscalationError("invalid_box")
    return (x0, y0, x1, y1)


def _size_px(box: Box, page_size_pt: tuple[float, float], dpi: int) -> tuple[int, int]:
    return raster_size_px(box, page_size_pt, dpi)


def _fits(w: int, h: int, policy: EscalationPolicy) -> bool:
    return w <= policy.max_side_px and h <= policy.max_side_px and w * h <= policy.max_pixels


def _expand(box: Box, margin: float) -> Box:
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * margin, (y1 - y0) * margin
    return (max(0.0, x0 - dx), max(0.0, y0 - dy), min(1.0, x1 + dx), min(1.0, y1 + dy))


def _tiles(box: Box, page_size_pt: tuple[float, float], dpi: int, policy: EscalationPolicy) -> tuple[Tile, ...]:
    """Cut ``box`` into the fewest equal-size grid tiles that each fit the per-image
    limits, with ``tile_overlap_px`` shared on inner seams. Empty when it cannot be
    done within ``max_tiles``."""
    width, height = _size_px(box, page_size_pt, dpi)
    scale = dpi / 72.0
    page_w_pt, page_h_pt = page_size_pt
    src_width = math.ceil(page_w_pt * scale)
    src_height = math.ceil(page_h_pt * scale)

    crop_left_0 = math.ceil(box[0] * page_w_pt * scale)
    crop_right_0 = math.ceil((1.0 - box[2]) * page_w_pt * scale)
    crop_top_0 = math.ceil(box[1] * page_h_pt * scale)
    crop_bottom_0 = math.ceil((1.0 - box[3]) * page_h_pt * scale)

    overlap = policy.tile_overlap_px
    for total in range(2, policy.max_tiles + 1):
        for nx in range(1, total + 1):
            if total % nx:
                continue
            ny = total // nx
            tw, th = math.ceil(width / nx), math.ceil(height / ny)
            ew, eh = tw + (2 * overlap if nx > 1 else 0), th + (2 * overlap if ny > 1 else 0)
            if not _fits(ew, eh, policy):
                continue
            tiles: list[Tile] = []
            for iy in range(ny):
                for ix in range(nx):
                    left = max(0, ix * tw - (overlap if ix else 0))
                    top = max(0, iy * th - (overlap if iy else 0))
                    right = min(width, (ix + 1) * tw + (overlap if ix < nx - 1 else 0))
                    bottom = min(height, (iy + 1) * th + (overlap if iy < ny - 1 else 0))

                    P_left = crop_left_0 + left
                    P_right = crop_left_0 + right
                    P_top = crop_top_0 + top
                    P_bottom = crop_top_0 + bottom

                    tile_x0 = box[0] if left == 0 else (P_left - 0.5) / (page_w_pt * scale)
                    tile_x1 = box[2] if right == width else 1.0 - (src_width - P_right - 0.5) / (page_w_pt * scale)
                    tile_y0 = box[1] if top == 0 else (P_top - 0.5) / (page_h_pt * scale)
                    tile_y1 = box[3] if bottom == height else 1.0 - (src_height - P_bottom - 0.5) / (page_h_pt * scale)

                    tile_box = (tile_x0, tile_y0, tile_x1, tile_y1)
                    tw_px, th_px = right - left, bottom - top
                    if not _fits(tw_px, th_px, policy):
                        return ()
                    tiles.append(Tile(tile_box, tw_px, th_px, (left, top)))
            return tuple(tiles)
    return ()


def _check_attempt(a: object, policy: EscalationPolicy) -> Attempt:
    if not isinstance(a, Attempt):
        raise EscalationError("invalid_history")
    if isinstance(a.dpi, bool) or not isinstance(a.dpi, int) or a.dpi not in (policy.first_dpi, policy.escalated_dpi):
        raise EscalationError("invalid_history")
    try:
        _check_box(a.box)
    except EscalationError:
        raise EscalationError("invalid_history")
    if isinstance(a.profile, bool) or not isinstance(a.profile, str) or not 1 <= len(a.profile) <= 64:
        raise EscalationError("invalid_history")
    if isinstance(a.pixels, bool) or not isinstance(a.pixels, int) or a.pixels <= 0:
        raise EscalationError("invalid_history")
    return a


def _render_plan(box, profile, dpi, attempt_no, page_size_pt, history, policy) -> Plan:
    width, height = _size_px(box, page_size_pt, dpi)
    if width <= 0 or height <= 0:
        return _review("unrenderable_crop")
    tiles: tuple[Tile, ...] = ()
    if not _fits(width, height, policy):
        tiles = _tiles(box, page_size_pt, dpi, policy)
        if not tiles:
            return _review("pixel_limit_exceeded")  # not downscaled, not retried
    pixels = sum(t.width_px * t.height_px for t in tiles) if tiles else width * height
    if sum(a.pixels for a in history) + pixels > policy.pixel_budget:
        return _review("pixel_budget_exceeded")
    return Plan("render", attempt_no, dpi, box, profile, width, height, tiles, ())


def plan_next(
    history: Sequence[Attempt],
    weakness: str,
    *,
    box: Box,
    page_size_pt: tuple[float, float],
    policy: EscalationPolicy = EscalationPolicy(),
    default_profile: str = "default",
) -> Plan:
    """The next step for one asset.

    ``history`` holds the attempts already made (empty before the first render);
    ``weakness`` says what was wrong with the last one (ignored for the first).
    ``box`` is the original crop. Never raises for a bad outcome: exhausting a
    remedy, the attempt cap or a limit returns ``needs_review`` with reasons.
    """
    if not isinstance(policy, EscalationPolicy):
        raise EscalationError("invalid_policy")
    if weakness not in WEAKNESSES:
        raise EscalationError("invalid_weakness")
    box = _check_box(box)
    if not isinstance(page_size_pt, tuple) or len(page_size_pt) != 2:
        raise EscalationError("invalid_page_size")
    for v in page_size_pt:
        if not _finite_number(v) or v <= 0:
            raise EscalationError("invalid_page_size")
        try:
            scaled = float(v) * (policy.escalated_dpi / 72.0)
            if not math.isfinite(scaled) or scaled > 1e9:
                raise EscalationError("invalid_page_size")
        except (OverflowError, ValueError):
            raise EscalationError("invalid_page_size")
    if not isinstance(default_profile, str) or not 1 <= len(default_profile) <= 64:
        raise EscalationError("invalid_profile")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise EscalationError("invalid_history")
    if len(history) > policy.max_attempts:
        raise EscalationError("invalid_history")
    prev_dpi = None
    for a in history:
        _check_attempt(a, policy)
        if prev_dpi is not None and a.dpi < prev_dpi:
            raise EscalationError("invalid_history")
        prev_dpi = a.dpi

    if not history:
        return _render_plan(box, default_profile, policy.first_dpi, 1, page_size_pt, history, policy)
    if weakness == "none":
        return Plan("accept", None, None, None, None, None, None, (), ())
    if len(history) >= policy.max_attempts:
        return _review("attempt_limit")

    last = history[-1]

    def plan(dpi: int, new_box: Box, profile: str) -> Plan:
        return _render_plan(new_box, profile, dpi, len(history) + 1, page_size_pt, history, policy)

    if weakness in ("too_small", "blurry"):
        if any(a.dpi >= policy.escalated_dpi for a in history):
            return _review("dpi_escalation_exhausted")
        return plan(policy.escalated_dpi, last.box, last.profile)

    # missing_labels: the original box widened once (never widened again), then each
    # alternate profile once on the last box.
    already_widened = any(_round_box(a.box) != _round_box(box) for a in history)
    wider = _expand(box, policy.expand_margin)
    if not already_widened and _round_box(wider) != _round_box(box):
        return plan(last.dpi, wider, last.profile)
    used_profiles = {a.profile for a in history}
    for profile in policy.alternate_profiles:
        if profile not in used_profiles:
            return plan(last.dpi, last.box, profile)
    return _review("expansion_exhausted")


def _round_box(box: Box) -> Box:
    return tuple(round(v, 9) for v in box)  # type: ignore[return-value]
