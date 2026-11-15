"""Chart axis calibration and read-value intervals (SPEC 19.3 items 4 and 5).

Pure functions: a pixel position on a chart axis becomes a data value with an
interval that is derived, never guessed. A value read from a plot is only ever
``estimated_from_plot`` (never an exact label) and carries the interval that
the pixel resolution allows. When the error cannot be bounded, reading raises
``AxisCalibrationError`` instead of returning a number: the caller must then
produce no precise numeric question and use a verified trend question or
``needs_review`` (SPEC 19.3 item 5).

Calibration uses two reference ticks on one axis (pixel -> label value), linear
or logarithmic. ``pixel_tolerance`` is the half-width of the pixel uncertainty
of every position involved (the read point and both ticks); the interval is the
worst case over all combinations, which is exact because the mapping is
monotone in each of the three positions. The tolerance is an input fixed from
the image resolution or a rubric before freeze; it is never widened after
seeing participant answers.

Numerics fail closed. Every input is normalized to a float (an integer beyond the
float range is a reason code, not an ``OverflowError``), and every intermediate and
final value, including the multiplier and ``difference``, must be finite, ordered
(``low <= value <= high``) and have a positive width; otherwise the result is
``AxisCalibrationError('unbounded_error')``. A pixel tolerance smaller than one float
step at the position's magnitude is refused, so a zero-width interval is never
published as an exact estimate.

Covered cases: linear and log axes, dual axes (a reading must name its axis),
truncated axes (flagged in the result), a label multiplier ("in thousands",
"in millions"), percent vs percentage point (a difference of two percentages
is in ``pp``) and error bars. Nothing here reads pixels from an image.
"""
from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
from typing import Iterable, Mapping

__all__ = [
    "AxisCalibration",
    "AxisCalibrationError",
    "ErrorBar",
    "ReadValue",
    "build_axes",
    "difference",
    "read_error_bar",
    "read_value",
]

SCALES = ("linear", "log", "unknown")
ESTIMATED = "estimated_from_plot"
DERIVED = "derived_calculation"


class AxisCalibrationError(ValueError):
    """A fixed reason code; never embeds caller data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AxisCalibrationError(code)
    try:
        result = float(value)  # an int beyond the float range raises OverflowError, not a reason code
    except OverflowError:
        raise AxisCalibrationError(code) from None
    if not math.isfinite(result):
        raise AxisCalibrationError(code)
    return result


def _finite(value: float) -> float:
    """Every intermediate and final result must be finite: a float that overflowed, or that a
    non-finite operand turned into inf or nan, is not a measurement."""
    if not math.isfinite(value):
        raise AxisCalibrationError("unbounded_error")
    return value


def _ordered(low: float, value: float, high: float) -> None:
    if not low <= value <= high:
        raise AxisCalibrationError("unbounded_error")


@dataclass(frozen=True)
class ReadValue:
    value: float
    low: float
    high: float
    unit: str | None
    value_source: str
    axis_id: str | None
    scale: str | None
    notes: tuple[str, ...] = ()

    def to_observation_fields(self) -> dict:
        """Fields for ``visual_observations``: an estimate stores its interval,
        any other source stores none (the table's CHECK constraint)."""
        if self.value_source == ESTIMATED:
            return {"value_source": ESTIMATED, "estimate_low": self.low, "estimate_high": self.high}
        return {"value_source": self.value_source, "estimate_low": None, "estimate_high": None}


@dataclass(frozen=True)
class AxisCalibration:
    axis_id: str
    scale: str
    pixel_a: float
    value_a: float
    pixel_b: float
    value_b: float
    pixel_tolerance: float
    unit: str | None = None
    multiplier: float = 1.0
    truncated: bool | None = None  # None: not known

    def __post_init__(self) -> None:
        if not isinstance(self.axis_id, str) or not 1 <= len(self.axis_id) <= 128:
            raise AxisCalibrationError("invalid_axis_id")
        if self.scale not in SCALES:
            raise AxisCalibrationError("invalid_scale")
        for name in ("pixel_a", "value_a", "pixel_b", "value_b"):
            object.__setattr__(self, name, _number(getattr(self, name), "invalid_number"))
        object.__setattr__(self, "pixel_tolerance", _number(self.pixel_tolerance, "invalid_tolerance"))
        object.__setattr__(self, "multiplier", _number(self.multiplier, "invalid_multiplier"))
        if self.pixel_tolerance <= 0:
            raise AxisCalibrationError("invalid_tolerance")
        if self.multiplier <= 0:
            raise AxisCalibrationError("invalid_multiplier")
        if self.unit is not None and (not isinstance(self.unit, str) or not 1 <= len(self.unit) <= 32):
            raise AxisCalibrationError("invalid_unit")
        if self.truncated is not None and not isinstance(self.truncated, bool):
            raise AxisCalibrationError("invalid_truncated")
        _finite(self.pixel_b - self.pixel_a)  # a span that overflows would silently read as zero
        if abs(self.pixel_b - self.pixel_a) <= 2 * self.pixel_tolerance:
            raise AxisCalibrationError("ticks_too_close")  # the tick error could flip the axis
        if self.value_a == self.value_b:
            raise AxisCalibrationError("ticks_equal_value")
        if self.scale == "log" and (self.value_a <= 0 or self.value_b <= 0):
            raise AxisCalibrationError("log_needs_positive_ticks")
        if self.scale == "linear":
            _finite(self.value_b - self.value_a)

    def _map(self, pixel: float, pa: float, pb: float) -> float:
        t = _finite(_finite(pixel - pa) / _finite(pb - pa))
        if self.scale == "linear":
            result = _finite(self.value_a + _finite(_finite(self.value_b - self.value_a) * t))
        else:
            la, lb = math.log(self.value_a), math.log(self.value_b)
            try:
                result = math.exp(_finite(la + _finite(_finite(lb - la) * t)))
            except OverflowError:
                raise AxisCalibrationError("unbounded_error") from None
            if result <= 0.0:  # underflow: a positive axis value cannot read as zero
                raise AxisCalibrationError("unbounded_error")
        return _finite(result)

    def read(self, pixel: float) -> ReadValue:
        """The value at ``pixel`` with its worst-case interval."""
        if self.scale == "unknown":
            raise AxisCalibrationError("unbounded_error")  # the axis law is unknown: no bounded error
        px = _number(pixel, "invalid_number")
        tol = self.pixel_tolerance
        for position in (px, self.pixel_a, self.pixel_b):
            # a tolerance below the float resolution at this magnitude would collapse the interval
            # to a point and publish a zero-width estimate that is not real
            if position + tol == position or position - tol == position:
                raise AxisCalibrationError("unbounded_error")
        low_px, high_px = sorted((self.pixel_a, self.pixel_b))
        if px < low_px - tol or px > high_px + tol:
            # two ticks do not bound an extrapolation
            raise AxisCalibrationError("outside_calibrated_range")
        candidates = [
            self._map(px + dp, self.pixel_a + da, self.pixel_b + db)
            for dp, da, db in itertools.product((-tol, tol), repeat=3)
        ]
        value = self._map(px, self.pixel_a, self.pixel_b)
        low, high = min(candidates), max(candidates)
        _ordered(low, value, high)
        if not low < high:  # positive pixel tolerance and distinct ticks always give a real width
            raise AxisCalibrationError("unbounded_error")
        m = self.multiplier
        value, low, high = _finite(value * m), _finite(low * m), _finite(high * m)
        _ordered(low, value, high)
        if not low < high:  # scaling must not squeeze the interval to a point either
            raise AxisCalibrationError("unbounded_error")
        notes: list[str] = []
        if self.truncated is True:
            notes.append("axis_truncated")
        elif self.truncated is None:
            notes.append("axis_truncation_unknown")
        return ReadValue(
            value=value,
            low=low,
            high=high,
            unit=self.unit,
            value_source=ESTIMATED,
            axis_id=self.axis_id,
            scale=self.scale,
            notes=tuple(notes),
        )


def build_axes(calibrations: Iterable[AxisCalibration]) -> dict[str, AxisCalibration]:
    axes: dict[str, AxisCalibration] = {}
    for cal in calibrations:
        if not isinstance(cal, AxisCalibration) or cal.axis_id in axes:
            raise AxisCalibrationError("invalid_axes")
        axes[cal.axis_id] = cal
    if not axes:
        raise AxisCalibrationError("invalid_axes")
    return axes


def read_value(axes: Mapping[str, AxisCalibration], axis_id: str | None, pixel: float) -> ReadValue:
    """Read ``pixel`` on the axis a series belongs to. With several axes (dual
    axis) the axis must be named; there is no default."""
    if axis_id is None:
        if len(axes) != 1:
            raise AxisCalibrationError("ambiguous_axis")
        (cal,) = axes.values()
    else:
        cal = axes.get(axis_id) if isinstance(axis_id, str) else None
        if cal is None:
            raise AxisCalibrationError("unknown_axis")
    return cal.read(pixel)


def difference(a: ReadValue, b: ReadValue) -> ReadValue:
    """``a - b`` with interval arithmetic. A difference of two percentages is in
    percentage points (``pp``); other units must match."""
    if not isinstance(a, ReadValue) or not isinstance(b, ReadValue):
        raise AxisCalibrationError("invalid_reading")
    if a.unit != b.unit:
        raise AxisCalibrationError("unit_mismatch")
    unit = "pp" if a.unit == "%" else a.unit
    value = _finite(_number(a.value, "invalid_number") - _number(b.value, "invalid_number"))
    low = _finite(_number(a.low, "invalid_number") - _number(b.high, "invalid_number"))
    high = _finite(_number(a.high, "invalid_number") - _number(b.low, "invalid_number"))
    _ordered(low, value, high)
    return ReadValue(
        value=value,
        low=low,
        high=high,
        unit=unit,
        value_source=DERIVED,
        axis_id=None,
        scale=None,
        notes=tuple(sorted(set(a.notes) | set(b.notes))),
    )


@dataclass(frozen=True)
class ErrorBar:
    value: ReadValue
    lower: ReadValue
    upper: ReadValue


def read_error_bar(cal: AxisCalibration, center_pixel: float, end_pixel_1: float, end_pixel_2: float) -> ErrorBar:
    """A mean with its error-bar ends, all on one axis. The ends are ordered by
    value; a centre outside them is refused."""
    centre = cal.read(center_pixel)
    ends = sorted((cal.read(end_pixel_1), cal.read(end_pixel_2)), key=lambda r: r.value)
    if not ends[0].value <= centre.value <= ends[1].value:
        raise AxisCalibrationError("inconsistent_error_bar")
    return ErrorBar(value=centre, lower=ends[0], upper=ends[1])
