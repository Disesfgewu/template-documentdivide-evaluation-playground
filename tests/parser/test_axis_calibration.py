"""Chart axis calibration and read-value intervals (SPEC 19.3 items 4-5, A08, T91, T100)."""
from __future__ import annotations

import math
import random
import unittest

from lab_parser.axis_calibration import (
    AxisCalibration,
    AxisCalibrationError,
    build_axes,
    difference,
    read_error_bar,
    read_value,
)


def cal(**kw) -> AxisCalibration:
    base = dict(axis_id="y", scale="linear", pixel_a=400.0, value_a=0.0, pixel_b=100.0, value_b=100.0,
                pixel_tolerance=0.5, unit="%", truncated=False)
    base.update(kw)
    return AxisCalibration(**base)


class ErrorCode:
    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(AxisCalibrationError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(str(ctx.exception), code)


class LinearTests(ErrorCode, unittest.TestCase):
    def test_midpoint_value_and_interval_are_worst_case_over_all_positions(self):
        r = cal().read(250.0)
        self.assertAlmostEqual(r.value, 50.0)
        # tolerance of the read point and of both ticks, all at once
        expected = [100 * ((250 + dp) - (400 + da)) / ((100 + db) - (400 + da)) for dp in (-.5, .5)
                    for da in (-.5, .5) for db in (-.5, .5)]
        self.assertAlmostEqual(r.low, min(expected))
        self.assertAlmostEqual(r.high, max(expected))
        self.assertLess(r.low, r.value)
        self.assertGreater(r.high, r.value)
        self.assertEqual((r.value_source, r.unit, r.axis_id, r.scale), ("estimated_from_plot", "%", "y", "linear"))

    def test_axis_direction_does_not_matter(self):
        up = cal(pixel_a=100.0, value_a=0.0, pixel_b=400.0, value_b=100.0).read(250.0)   # value grows with pixel
        down = cal().read(250.0)                                                          # value shrinks with pixel
        self.assertAlmostEqual(up.value, down.value)
        self.assertAlmostEqual(up.low, down.low)
        self.assertAlmostEqual(up.high, down.high)
        self.assertLessEqual(up.low, up.value)

    def test_tolerance_widens_the_interval(self):
        narrow, wide = cal(pixel_tolerance=0.5).read(300.0), cal(pixel_tolerance=2.0).read(300.0)
        self.assertGreater(wide.high - wide.low, narrow.high - narrow.low)

    def test_true_value_is_inside_for_any_noise_within_tolerance(self):
        rng = random.Random(7)
        for _ in range(300):
            slope, offset = rng.uniform(-5, 5) or 1.0, rng.uniform(-100, 100)
            pa, pb = rng.uniform(0, 200), rng.uniform(300, 900)
            tol = rng.uniform(0.1, 3.0)
            truth = lambda p: slope * p + offset
            noisy = lambda p: p + rng.uniform(-tol, tol)
            c = AxisCalibration("a", "linear", noisy(pa), truth(pa), noisy(pb), truth(pb), tol)
            p_true = rng.uniform(pa, pb)
            r = c.read(noisy(p_true))
            self.assertLessEqual(r.low - 1e-6, truth(p_true))
            self.assertGreaterEqual(r.high + 1e-6, truth(p_true))

    def test_multiplier_scales_value_and_interval(self):
        base, thousands = cal(unit=None).read(250.0), cal(unit=None, multiplier=1000.0).read(250.0)
        self.assertAlmostEqual(thousands.value, base.value * 1000)
        self.assertAlmostEqual(thousands.low, base.low * 1000)
        self.assertAlmostEqual(thousands.high, base.high * 1000)
        self.assertAlmostEqual(cal(multiplier=1e6).read(250.0).value, 50e6)

    def test_negative_values_and_zero_crossing(self):
        r = cal(value_a=-50.0, value_b=50.0).read(250.0)
        self.assertAlmostEqual(r.value, 0.0)
        self.assertTrue(r.low < 0.0 < r.high)


class LogTests(ErrorCode, unittest.TestCase):
    def test_decades_are_equally_spaced(self):
        c = cal(scale="log", value_a=1.0, value_b=1000.0, unit=None)
        self.assertAlmostEqual(c.read(300.0).value, 10.0)
        self.assertAlmostEqual(c.read(200.0).value, 100.0)
        self.assertAlmostEqual(c.read(400.0).value, 1.0)

    def test_interval_is_multiplicative_and_contains_value(self):
        r = cal(scale="log", value_a=1.0, value_b=1000.0, unit=None).read(250.0)
        self.assertTrue(r.low < r.value < r.high)
        self.assertAlmostEqual(r.value, 10 ** 1.5)
        self.assertAlmostEqual(r.high / r.value, r.value / r.low, places=1)

    def test_log_refuses_non_positive_ticks(self):
        for a, b in ((0.0, 10.0), (-1.0, 10.0), (1.0, 0.0)):
            self.assertCode("log_needs_positive_ticks", cal, scale="log", value_a=a, value_b=b)

    def test_linear_reading_of_a_log_axis_would_differ(self):
        log = cal(scale="log", value_a=1.0, value_b=1000.0, unit=None).read(300.0).value
        lin = cal(scale="linear", value_a=1.0, value_b=1000.0, unit=None).read(300.0).value
        self.assertGreater(abs(log - lin), 100)


class RefusalTests(ErrorCode, unittest.TestCase):
    def test_unknown_scale_gives_no_number(self):
        self.assertCode("unbounded_error", cal(scale="unknown").read, 250.0)

    def test_invalid_scale_value(self):
        self.assertCode("invalid_scale", cal, scale="sqrt")

    def test_outside_calibrated_range_is_not_extrapolated(self):
        c = cal()
        self.assertCode("outside_calibrated_range", c.read, 50.0)
        self.assertCode("outside_calibrated_range", c.read, 450.0)
        c.read(100.0), c.read(400.0), c.read(99.6)  # at the ticks, within tolerance

    def test_ticks_too_close_to_bound_the_error(self):
        self.assertCode("ticks_too_close", cal, pixel_a=100.0, pixel_b=100.9)  # <= 2 * tolerance
        cal(pixel_a=100.0, pixel_b=101.1)
        self.assertCode("ticks_equal_value", cal, value_a=5.0, value_b=5.0)

    def test_non_finite_and_wrong_types(self):
        for bad in (math.nan, math.inf, -math.inf, True, "3", None):
            with self.subTest(bad=bad):
                self.assertCode("invalid_number", cal().read, bad)
                self.assertCode("invalid_number", cal, pixel_a=bad)
        for tol in (0, -1, math.nan, True):
            self.assertCode("invalid_tolerance", cal, pixel_tolerance=tol)
        for mult in (0, -2.0, math.inf):
            self.assertCode("invalid_multiplier", cal, multiplier=mult)
        self.assertCode("invalid_axis_id", cal, axis_id="")
        self.assertCode("invalid_unit", cal, unit="")
        self.assertCode("invalid_truncated", cal, truncated="yes")

    def test_overflow_is_reported_not_returned(self):
        # 1e-300 .. 1e300 over a 1 px span: any reading inside the span stays finite,
        # but a span this steep with a tolerance that fits in it must not overflow silently.
        steep = AxisCalibration("h", "log", 0.0, 1e-300, 100.0, 1e300, 1.0)
        self.assertTrue(math.isfinite(steep.read(50.0).value))
        self.assertCode("unbounded_error", AxisCalibration("h", "log", 0.0, 1e-300, 3.0, 1e300, 1.0).read, 3.0)


class DualAxisAndFlagsTests(ErrorCode, unittest.TestCase):
    def setUp(self):
        self.axes = build_axes([cal(axis_id="left", unit="USD"),
                                cal(axis_id="right", value_a=0.0, value_b=1.0, unit="%")])

    def test_each_series_reads_its_own_axis(self):
        self.assertAlmostEqual(read_value(self.axes, "left", 250.0).value, 50.0)
        self.assertAlmostEqual(read_value(self.axes, "right", 250.0).value, 0.5)
        self.assertEqual(read_value(self.axes, "right", 250.0).unit, "%")

    def test_dual_axis_requires_an_explicit_axis(self):
        self.assertCode("ambiguous_axis", read_value, self.axes, None, 250.0)
        self.assertCode("unknown_axis", read_value, self.axes, "third", 250.0)
        self.assertCode("unknown_axis", read_value, self.axes, 3, 250.0)

    def test_single_axis_needs_no_name(self):
        self.assertAlmostEqual(read_value(build_axes([cal()]), None, 250.0).value, 50.0)

    def test_build_axes_refuses_duplicates_and_empty(self):
        self.assertCode("invalid_axes", build_axes, [cal(), cal()])
        self.assertCode("invalid_axes", build_axes, [])
        self.assertCode("invalid_axes", build_axes, ["not a calibration"])

    def test_truncation_is_flagged_never_hidden(self):
        self.assertEqual(cal(truncated=True).read(250.0).notes, ("axis_truncated",))
        self.assertEqual(cal(truncated=None).read(250.0).notes, ("axis_truncation_unknown",))
        self.assertEqual(cal(truncated=False).read(250.0).notes, ())


class DifferenceAndErrorBarTests(ErrorCode, unittest.TestCase):
    def test_percent_difference_is_percentage_points(self):
        c = cal()
        d = difference(c.read(150.0), c.read(300.0))
        self.assertEqual(d.unit, "pp")
        self.assertAlmostEqual(d.value, 83.33333333333333 - 33.333333333333336)
        a, b = c.read(150.0), c.read(300.0)
        self.assertAlmostEqual(d.low, a.low - b.high)
        self.assertAlmostEqual(d.high, a.high - b.low)
        self.assertEqual(d.value_source, "derived_calculation")

    def test_other_units_keep_their_unit_and_mismatch_is_refused(self):
        usd = cal(unit="USD")
        self.assertEqual(difference(usd.read(150.0), usd.read(300.0)).unit, "USD")
        self.assertCode("unit_mismatch", difference, usd.read(150.0), cal(unit="%").read(300.0))
        self.assertCode("unit_mismatch", difference, usd.read(150.0), cal(unit=None).read(300.0))

    def test_difference_interval_is_wider_than_either_input(self):
        c = cal()
        a, b = c.read(150.0), c.read(300.0)
        d = difference(a, b)
        self.assertGreater(d.high - d.low, (a.high - a.low))

    def test_notes_are_carried_into_a_difference(self):
        d = difference(cal(truncated=True).read(150.0), cal(truncated=None).read(300.0))
        self.assertEqual(d.notes, ("axis_truncated", "axis_truncation_unknown"))

    def test_error_bar_ends_are_ordered_and_contain_the_mean(self):
        bar = read_error_bar(cal(), 250.0, 200.0, 300.0)  # ends given in pixel order, not value order
        self.assertTrue(bar.lower.value < bar.value.value < bar.upper.value)
        self.assertAlmostEqual(bar.lower.value, 100 * (400 - 300) / 300)
        self.assertAlmostEqual(bar.upper.value, 100 * (400 - 200) / 300)

    def test_error_bar_with_centre_outside_is_refused(self):
        self.assertCode("inconsistent_error_bar", read_error_bar, cal(), 150.0, 200.0, 300.0)


class ObservationFieldsTests(unittest.TestCase):
    def test_estimate_stores_its_interval_as_the_table_requires(self):
        f = cal().read(250.0).to_observation_fields()
        self.assertEqual(f["value_source"], "estimated_from_plot")
        self.assertLessEqual(f["estimate_low"], f["estimate_high"])
        self.assertIsNotNone(f["estimate_low"])

    def test_derived_value_stores_no_interval_columns(self):
        c = cal()
        f = difference(c.read(150.0), c.read(300.0)).to_observation_fields()
        self.assertEqual(f, {"value_source": "derived_calculation", "estimate_low": None, "estimate_high": None})


if __name__ == "__main__":
    unittest.main()
