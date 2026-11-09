"""Unit tests for user-space to normalized SourceRegion coordinate transforms (SPEC 4.1 coordinates, T05, 19.2 L0, 19.3.8 limits)."""

from __future__ import annotations

import unittest

from lab_parser.geometry import to_region_space, to_user_space


class TestGeometry(unittest.TestCase):
    def assert_box_almost_equal(
        self,
        actual: tuple[float, float, float, float],
        expected: tuple[float, float, float, float],
        places: int = 9,
    ) -> None:
        for idx, (a, e) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                a,
                e,
                places=places,
                msg=f"Coordinate {idx} mismatch: actual {a} != expected {e}",
            )

    def test_to_region_space_rotations(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        b = (20.0, 10.0, 60.0, 30.0)

        cases = [
            (0, (0.1, 0.7, 0.3, 0.9)),
            (90, (0.1, 0.1, 0.3, 0.3)),
            (180, (0.7, 0.1, 0.9, 0.3)),
            (270, (0.7, 0.7, 0.9, 0.9)),
            (-90, (0.7, 0.7, 0.9, 0.9)),
            (450, (0.1, 0.1, 0.3, 0.3)),
        ]
        for rot, expected in cases:
            with self.subTest(rot=rot):
                res = to_region_space(b, c, rot)
                self.assert_box_almost_equal(res, expected)

    def test_offset_cropbox(self) -> None:
        c2 = (50.0, 50.0, 250.0, 150.0)
        b2 = (70.0, 60.0, 110.0, 80.0)

        cases = [
            (0, (0.1, 0.7, 0.3, 0.9)),
            (90, (0.1, 0.1, 0.3, 0.3)),
            (180, (0.7, 0.1, 0.9, 0.3)),
            (270, (0.7, 0.7, 0.9, 0.9)),
        ]
        for rot, expected in cases:
            with self.subTest(rot=rot):
                res = to_region_space(b2, c2, rot)
                self.assert_box_almost_equal(res, expected)

    def test_clipping(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        res = to_region_space((-10.0, 10.0, 60.0, 30.0), c, 0)
        self.assert_box_almost_equal(res, (0.0, 0.7, 0.3, 0.9))

    def test_error_outside_cropbox(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        with self.assertRaises(ValueError) as ctx:
            to_region_space((300.0, 10.0, 400.0, 30.0), c, 0)
        self.assertIn("outside_cropbox", str(ctx.exception))

    def test_error_invalid_rotate(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        b = (20.0, 10.0, 60.0, 30.0)
        for invalid_rot in (45, True, "90", 90.0):
            with self.subTest(rot=invalid_rot):
                with self.assertRaises(ValueError) as ctx:
                    to_region_space(b, c, invalid_rot)  # type: ignore[arg-type]
                self.assertTrue(str(ctx.exception).startswith("invalid_rotate"))

    def test_error_invalid_cropbox(self) -> None:
        b = (20.0, 10.0, 60.0, 30.0)

        with self.assertRaises(ValueError) as ctx:
            to_region_space(b, (0.0, 0.0, 0.0, 100.0), 0)
        self.assertIn("zero_area_cropbox", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            to_region_space(b, (0.0, 0.0, float("nan"), 100.0), 0)
        self.assertIn("invalid_cropbox", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            to_region_space(b, (0.0, 0.0, 100.0), 0)  # type: ignore[arg-type]
        self.assertIn("invalid_cropbox", str(ctx.exception))

    def test_error_invalid_box(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        with self.assertRaises(ValueError) as ctx:
            to_region_space((60.0, 10.0, 20.0, 30.0), c, 0)
        self.assertIn("invalid_box", str(ctx.exception))

    def test_error_invalid_region_user_space(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        with self.assertRaises(ValueError) as ctx:
            to_user_space((0.1, 0.1, 1.2, 0.3), c, 0)
        self.assertIn("invalid_region", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            to_user_space((-0.1, 0.1, 0.2, 0.3), c, 0)
        self.assertIn("invalid_region", str(ctx.exception))

    def test_round_trip(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        boxes_c = [
            (20.0, 10.0, 60.0, 30.0),
            (1.0, 1.0, 199.0, 99.0),
            (50.0, 20.0, 150.0, 80.0),
            (10.0, 40.0, 30.0, 90.0),
        ]

        c2 = (50.0, 50.0, 250.0, 150.0)
        boxes_c2 = [
            (70.0, 60.0, 110.0, 80.0),
            (51.0, 51.0, 249.0, 149.0),
            (100.0, 70.0, 200.0, 130.0),
            (60.0, 90.0, 80.0, 140.0),
        ]

        for cropbox, box_list in [(c, boxes_c), (c2, boxes_c2)]:
            for rot in (0, 90, 180, 270):
                for b in box_list:
                    with self.subTest(cropbox=cropbox, rot=rot, box=b):
                        reg = to_region_space(b, cropbox, rot)
                        rec = to_user_space(reg, cropbox, rot)
                        self.assert_box_almost_equal(rec, b, places=9)

    def test_to_user_space_exact(self) -> None:
        c = (0.0, 0.0, 200.0, 100.0)
        res = to_user_space((0.1, 0.7, 0.3, 0.9), c, 0)
        self.assert_box_almost_equal(res, (20.0, 10.0, 60.0, 30.0), places=9)


if __name__ == "__main__":
    unittest.main()
