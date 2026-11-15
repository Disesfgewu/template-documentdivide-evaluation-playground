import json
import random
import unittest
from fractions import Fraction as F

from lab_contracts.benchmark import TextCheck
from lab_scoring.units import UNITS_REVISION, UnitError, check_numeric, convert, parse_unit


def check(**kw):
    base = dict(check_id="c1", fact_id="f1", kind="numeric", expected=None, interval=None, unit=None,
                normalization_rule=None, absolute_tolerance=None, relative_tolerance=None, precision=None,
                supporting_evidence_ids=["e1"])
    base.update(kw)
    return TextCheck.model_validate_json(json.dumps(base))


class ConvertTests(unittest.TestCase):
    def test_scales_and_si_are_exact(self):
        self.assertEqual(convert("1.5", "million", "thousand"), F(1500))
        self.assertEqual(convert("3", "億", "萬"), F(30000))
        self.assertEqual(convert("2.5", "million USD", "USD"), F(2500000))
        self.assertEqual(convert("12.5", "%", "fraction"), F(1, 8))
        self.assertEqual(convert("250", "mm", "m"), F(1, 4))
        self.assertEqual(convert("90", "min", "h"), F(3, 2))

    def test_percent_and_percentage_points_do_not_mix(self):
        with self.assertRaises(UnitError) as cm:
            convert("2", "pp", "%")
        self.assertEqual(cm.exception.code, "unit_mismatch")

    def test_currencies_never_convert_into_each_other(self):
        with self.assertRaises(UnitError) as cm:
            convert("1", "USD", "TWD")
        self.assertEqual(cm.exception.code, "unit_mismatch")
        self.assertEqual(convert("3", "千 TWD", "TWD"), F(3000))

    def test_unsupported_units(self):
        for u in ("°C", "furlong", " kg", "kg ", "", "million %", "thousand km", "million million"):
            with self.subTest(u=u), self.assertRaises(UnitError) as cm:
                parse_unit(u)
            self.assertEqual(cm.exception.code, "unsupported_unit")

    def test_floats_refused(self):
        with self.assertRaisesRegex(ValueError, "not a float"):
            convert(0.1, "m", "cm")

    def test_round_trip_is_identity(self):
        rng = random.Random(99)
        groups = [["count", "thousand", "萬", "million", "億", "billion"], ["fraction", "%", "‰"],
                  ["m", "km", "cm", "mm"], ["g", "kg", "t"], ["s", "min", "h"]]
        for _ in range(200):
            g = rng.choice(groups)
            a, b = rng.choice(g), rng.choice(g)
            v = F(rng.randint(-10**6, 10**6), rng.randint(1, 1000))
            self.assertEqual(convert(convert(v, a, b), b, a), v)


class CheckNumericTests(unittest.TestCase):
    def test_conversion_happens_before_tolerance(self):
        c = check(expected=0.125, unit="fraction", absolute_tolerance=0.0)
        out = check_numeric(c, "12.5", "%")
        self.assertEqual((out.passed, out.reason, out.converted, out.units_revision),
                         (True, None, F(1, 8), UNITS_REVISION))

    def test_wrong_unit_fails_even_if_number_is_close(self):
        c = check(expected=12.5, unit="%", absolute_tolerance=0.5)
        self.assertEqual(check_numeric(c, "12.5", "pp").reason, "unit_mismatch")
        self.assertEqual(check_numeric(c, "12.5", "°C").reason, "unsupported_unit")
        self.assertEqual(check_numeric(c, "12.5", None).reason, "unit_missing")
        c2 = check(expected=3.0, absolute_tolerance=0.0)
        self.assertEqual(check_numeric(c2, "3", "kg").reason, "unexpected_unit")
        self.assertTrue(check_numeric(c2, "3", None).passed)

    def test_t99_tolerance_bounds_are_inclusive_and_exact(self):
        # |actual - 100| <= max(0.5, 0.01*100) = 1  (in million TWD)
        c = check(expected=100.0, unit="million TWD", absolute_tolerance=0.5, relative_tolerance=0.01)
        self.assertTrue(check_numeric(c, "101", "million TWD").passed)
        self.assertTrue(check_numeric(c, "99000000", "TWD").passed)       # exactly the lower bound
        out = check_numeric(c, "98999999", "TWD")
        self.assertEqual((out.passed, out.reason), (False, "outside_tolerance"))

    def test_relative_zero_needs_absolute(self):
        c = check(expected=0.0, unit="%", absolute_tolerance=0.1)
        self.assertTrue(check_numeric(c, "0.001", "fraction").passed)   # 0.1 %
        self.assertFalse(check_numeric(c, "0.0011", "fraction").passed)

    def test_interval_is_not_widened(self):
        c = check(interval=[10.0, 20.0], unit="km")
        self.assertTrue(check_numeric(c, "20000", "m").passed)
        self.assertFalse(check_numeric(c, "20000.001", "m").passed)

    def test_agrees_with_contract_for_same_unit(self):
        # Without conversion the engine must match TextCheck.numeric_matches.
        rng = random.Random(7)
        for _ in range(300):
            exp = rng.choice([0.0, 1.0, 100.0, -3.5, 1234.25])
            c = check(expected=exp, unit="m", absolute_tolerance=rng.choice([0.0, 0.25, 1.0]),
                      relative_tolerance=rng.choice([None, 0.01, 0.1]))
            actual = F(rng.randint(-4000, 4000), 4) / 1  # quarter steps are exact floats
            with self.subTest(exp=exp, actual=actual):
                self.assertEqual(check_numeric(c, actual, "m").passed, c.numeric_matches(float(actual)))

    def test_non_numeric_check_rejected(self):
        c = TextCheck.model_validate_json(json.dumps(dict(
            check_id="c", fact_id="f", kind="quote", expected="x", interval=None, unit=None,
            normalization_rule=None, absolute_tolerance=None, relative_tolerance=None, precision=None,
            supporting_evidence_ids=["e"])))
        with self.assertRaises(ValueError):
            check_numeric(c, "1", None)


if __name__ == "__main__":
    unittest.main()
