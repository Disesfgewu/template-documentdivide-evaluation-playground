"""Deterministic unit conversion for numeric checks (SPEC 19.5, T39, T99).

The conversion table is frozen and versioned. A conversion that is not in
the table is "unsupported", never guessed: an answer in the wrong unit fails
even when its number happens to be close. Percent and percentage points are
different dimensions (SPEC 19.3), and so is every currency (no exchange
rates). Affine units such as temperature are deliberately absent."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Union

from lab_contracts.benchmark import TextCheck

UNITS_REVISION = "lab-units-1"

# Scale words: a multiplier that may stand alone (a count) or prefix a base
# unit ("million USD", "千 TWD").
SCALES: dict[str, Fraction] = {
    "thousand": Fraction(10**3), "千": Fraction(10**3),
    "萬": Fraction(10**4), "万": Fraction(10**4),
    "million": Fraction(10**6), "百萬": Fraction(10**6), "百万": Fraction(10**6),
    "億": Fraction(10**8), "亿": Fraction(10**8),
    "billion": Fraction(10**9), "十億": Fraction(10**9), "十亿": Fraction(10**9),
}

# base unit -> (dimension, factor to the dimension's base)
BASE_UNITS: dict[str, tuple[str, Fraction]] = {
    "count": ("count", Fraction(1)),
    "fraction": ("ratio", Fraction(1)), "%": ("ratio", Fraction(1, 100)), "‰": ("ratio", Fraction(1, 1000)),
    "pp": ("percentage_point", Fraction(1)),
    "m": ("length", Fraction(1)), "km": ("length", Fraction(1000)),
    "cm": ("length", Fraction(1, 100)), "mm": ("length", Fraction(1, 1000)),
    "g": ("mass", Fraction(1)), "kg": ("mass", Fraction(1000)), "t": ("mass", Fraction(10**6)),
    "s": ("time", Fraction(1)), "min": ("time", Fraction(60)), "h": ("time", Fraction(3600)),
}
# ISO 4217 codes each form their own dimension; only scale converts them.
CURRENCIES = frozenset({"TWD", "USD", "EUR", "JPY", "CNY", "HKD", "GBP"})


class UnitError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


def parse_unit(unit: str) -> tuple[str, Fraction]:
    """(dimension, factor) for a unit string. Raises UnitError('unsupported_unit')."""
    if not isinstance(unit, str) or not unit:
        raise UnitError("unsupported_unit", repr(unit))
    if unit in SCALES:
        return "count", SCALES[unit]
    scale, base = Fraction(1), unit
    head, sep, tail = unit.partition(" ")
    if sep and head in SCALES:
        scale, base = SCALES[head], tail
    if base in CURRENCIES:
        return f"currency:{base}", scale
    if base in BASE_UNITS:
        dim, factor = BASE_UNITS[base]
        if sep and dim not in ("count",):
            # "million %" or "thousand km" are not supported spellings.
            raise UnitError("unsupported_unit", repr(unit))
        return dim, scale * factor
    raise UnitError("unsupported_unit", repr(unit))


Number = Union[Fraction, int, str]


def exact(value: Number) -> Fraction:
    """Exact value from an int, Fraction or decimal string ("12.30"). Floats
    are refused: their binary value is not the number the answer wrote."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("pass an exact number (int, Fraction or decimal string), not a float")
    if isinstance(value, (int, Fraction)):
        return Fraction(value)
    if isinstance(value, str):
        try:
            return Fraction(value.strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    raise ValueError(f"unsupported number type {type(value).__name__}")


def convert(value: Number, from_unit: str, to_unit: str) -> Fraction:
    d_from, f_from = parse_unit(from_unit)
    d_to, f_to = parse_unit(to_unit)
    if d_from != d_to:
        raise UnitError("unit_mismatch", f"{from_unit} ({d_from}) -> {to_unit} ({d_to})")
    return exact(value) * f_from / f_to


@dataclass(frozen=True)
class NumericOutcome:
    passed: bool
    reason: Optional[str]          # None when passed
    converted: Optional[Fraction]  # actual value in the check's unit
    units_revision: str = UNITS_REVISION


def check_numeric(check: TextCheck, actual: Number, actual_unit: Optional[str]) -> NumericOutcome:
    """Convert first, then apply the frozen tolerance or interval (SPEC 19.5)."""
    if check.kind != "numeric":
        raise ValueError("check_numeric applies to numeric checks only")
    value = exact(actual)
    if check.unit is None:
        if actual_unit is not None:
            return NumericOutcome(False, "unexpected_unit", None)
        converted = value
    else:
        if actual_unit is None:
            return NumericOutcome(False, "unit_missing", None)
        try:
            converted = convert(value, actual_unit, check.unit)
        except UnitError as err:
            return NumericOutcome(False, err.code, None)
    if check.interval is not None:
        lo, hi = (Fraction(v) for v in check.interval)
        ok = lo <= converted <= hi
    else:
        exp = Fraction(check.expected)
        tol = max(Fraction(check.absolute_tolerance or 0), Fraction(check.relative_tolerance or 0) * abs(exp))
        ok = abs(converted - exp) <= tol
    return NumericOutcome(ok, None if ok else "outside_tolerance", converted)
