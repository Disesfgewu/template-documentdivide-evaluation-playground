"""Limited DPI/crop escalation planning (SPEC 19.3 item 8)."""
from __future__ import annotations

import io
import random
import unittest

import pypdfium2 as pdfium

from lab_parser.escalation import (
    Attempt, EscalationError, EscalationPolicy, WEAKNESSES, plan_next,
)
from lab_parser.render import MAX_PIXELS, MAX_SIDE_PX, render_region

PAGE = (612.0, 792.0)
BOX = (0.1, 0.1, 0.5, 0.5)


def attempt_of(plan, profile="default"):
    return Attempt(plan.dpi, plan.box, plan.profile or profile, plan.width_px * plan.height_px)


class ErrorCode:
    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(EscalationError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)


class FirstAttemptTests(unittest.TestCase):
    def test_first_render_is_150_dpi_with_the_pixels_render_will_produce(self):
        p = plan_next([], "none", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.attempt_no, p.dpi, p.box, p.profile), ("render", 1, 150, BOX, "default"))
        # Exact PDFium crop quantization at 150 DPI produces (509, 659)
        self.assertEqual((p.width_px, p.height_px), (509, 659))
        self.assertEqual(p.tiles, ())
        self.assertEqual(p.policy_version, "dpi-escalation-2")

    def test_weakness_is_ignored_for_the_first_render(self):
        self.assertEqual(plan_next([], "blurry", box=BOX, page_size_pt=PAGE).dpi, 150)

    def test_quantized_empty_crop_requires_review(self):
        p = plan_next([], "none", box=(0.5, 0.5, 0.5001, 0.5001), page_size_pt=(10.0, 10.0))
        self.assertEqual((p.action, p.reasons), ("needs_review", ("unrenderable_crop",)))


class DpiEscalationTests(ErrorCode, unittest.TestCase):
    def first(self):
        p = plan_next([], "none", box=BOX, page_size_pt=PAGE)
        return [attempt_of(p)]

    def test_too_small_or_blurry_escalates_once_to_300_with_the_same_box_and_profile(self):
        for weakness in ("too_small", "blurry"):
            with self.subTest(weakness=weakness):
                p = plan_next(self.first(), weakness, box=BOX, page_size_pt=PAGE)
                self.assertEqual((p.action, p.attempt_no, p.dpi, p.box, p.profile), ("render", 2, 300, BOX, "default"))
                # Exact PDFium crop quantization at 300 DPI produces (1019, 1319)
                self.assertEqual((p.width_px, p.height_px), (1019, 1319))

    def test_no_second_dpi_escalation(self):
        h = self.first()
        h.append(attempt_of(plan_next(h, "blurry", box=BOX, page_size_pt=PAGE)))
        p = plan_next(h, "blurry", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("dpi_escalation_exhausted",)))
        p = plan_next(h, "too_small", box=BOX, page_size_pt=PAGE)
        self.assertEqual(p.reasons, ("dpi_escalation_exhausted",))

    def test_accept_ends_the_loop(self):
        p = plan_next(self.first(), "none", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.dpi, p.tiles), ("accept", None, ()))

    def test_every_attempt_keeps_its_own_pixels_and_does_not_replace_the_earlier_one(self):
        h = self.first()
        p2 = plan_next(h, "blurry", box=BOX, page_size_pt=PAGE)
        self.assertNotEqual(p2.width_px * p2.height_px, h[0].pixels)  # the caller stores both


class MissingLabelsTests(unittest.TestCase):
    def test_wider_box_first_clipped_to_the_page(self):
        h = [Attempt(150, BOX, "default", 100)]
        p = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.dpi, p.profile), ("render", 150, "default"))
        self.assertAlmostEqual(p.box[0], 0.1 - 0.04)
        self.assertAlmostEqual(p.box[2], 0.5 + 0.04)
        edge = (0.0, 0.0, 0.5, 0.5)
        p = plan_next([Attempt(150, edge, "default", 100)], "missing_labels", box=edge, page_size_pt=PAGE)
        self.assertEqual((p.box[0], p.box[1]), (0.0, 0.0))
        self.assertAlmostEqual(p.box[2], 0.5 + 0.1 * 0.5)

    def test_then_each_alternate_profile_once_then_review(self):
        pol = EscalationPolicy(alternate_profiles=("ocr_sharp", "vlm_wide"), max_attempts=5)
        h = [Attempt(150, BOX, "default", 100)]
        p = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE, policy=pol)
        h.append(Attempt(p.dpi, p.box, p.profile, 100))
        p = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual((p.profile, p.box), ("ocr_sharp", h[-1].box))
        h.append(Attempt(p.dpi, p.box, p.profile, 100))
        p = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual(p.profile, "vlm_wide")
        h.append(Attempt(p.dpi, p.box, p.profile, 100))
        p = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("expansion_exhausted",)))

    def test_the_box_is_widened_only_once(self):
        h = [Attempt(150, BOX, "default", 100)]
        widened = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE)
        h.append(Attempt(widened.dpi, widened.box, widened.profile, 100))
        again = plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE, policy=EscalationPolicy(max_attempts=6))
        self.assertEqual((again.action, again.reasons), ("needs_review", ("expansion_exhausted",)))

    def test_a_full_page_box_cannot_widen(self):
        full = (0.0, 0.0, 1.0, 1.0)
        p = plan_next([Attempt(150, full, "default", 100)], "missing_labels", box=full, page_size_pt=PAGE)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("expansion_exhausted",)))


class BoundTests(ErrorCode, unittest.TestCase):
    def test_attempt_limit(self):
        h = [Attempt(150, BOX, "a", 1), Attempt(300, BOX, "a", 1), Attempt(300, (0, 0, 1, 1), "a", 1)]
        p = plan_next(h, "blurry", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("attempt_limit",)))
        longer = plan_next(h[:2], "missing_labels", box=BOX, page_size_pt=PAGE, policy=EscalationPolicy(max_attempts=4))
        self.assertEqual(longer.action, "render")

    def test_the_loop_always_terminates_within_the_attempt_cap(self):
        rng = random.Random(3)
        for _ in range(100):
            pol = EscalationPolicy(alternate_profiles=("p1", "p2"), max_attempts=rng.randint(1, 6))
            h, steps = [], 0
            while True:
                weakness = "none" if not h else rng.choice(WEAKNESSES[1:])
                p = plan_next(h, weakness, box=BOX, page_size_pt=PAGE, policy=pol)
                if p.action != "render":
                    break
                h.append(Attempt(p.dpi, p.box, p.profile, p.width_px * p.height_px))
                steps += 1
                self.assertLessEqual(steps, pol.max_attempts)
            self.assertLessEqual(len(h), pol.max_attempts)

    def test_a_dpi_already_used_is_not_planned_again(self):
        # the caller already rendered at 300 DPI, whatever the order: no further DPI step
        h = [Attempt(300, BOX, "default", 1)]
        self.assertEqual(plan_next(h, "blurry", box=BOX, page_size_pt=PAGE).reasons, ("dpi_escalation_exhausted",))
        self.assertEqual(plan_next(h, "missing_labels", box=BOX, page_size_pt=PAGE).dpi, 300)  # the box step keeps it

    def test_pixel_budget_is_the_cost_bound(self):
        pol = EscalationPolicy(pixel_budget=MAX_PIXELS)
        h = [Attempt(150, BOX, "default", MAX_PIXELS - 10)]
        p = plan_next(h, "blurry", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("pixel_budget_exceeded",)))
        ok = plan_next([Attempt(150, BOX, "default", 1000)], "blurry", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual(ok.action, "render")

    def test_limits_can_be_lowered_but_not_raised(self):
        EscalationPolicy(max_pixels=1_000_000, max_side_px=1000)
        for kwargs in ({"max_pixels": MAX_PIXELS + 1}, {"max_side_px": MAX_SIDE_PX + 1}, {"first_dpi": 300},
                       {"escalated_dpi": 301}, {"first_dpi": 71}, {"max_attempts": 0}, {"max_attempts": 7},
                       {"expand_margin": 0}, {"expand_margin": 0.6}, {"pixel_budget": 1}, {"max_tiles": 0},
                       {"tile_overlap_px": -1}, {"tile_overlap_px": 3000}, {"alternate_profiles": ("a", "a")},
                       {"alternate_profiles": ("",)}, {"first_dpi": True}):
            with self.subTest(kwargs=kwargs):
                self.assertCode("invalid_policy", EscalationPolicy, **kwargs)

    def test_a_lowered_limit_makes_the_planner_tile_sooner(self):
        pol = EscalationPolicy(max_pixels=100_000, max_side_px=400, tile_overlap_px=8)
        p = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=(300.0, 300.0), policy=pol)
        self.assertEqual(p.action, "render")
        self.assertGreater(len(p.tiles), 1)
        for t in p.tiles:
            self.assertLessEqual(max(t.width_px, t.height_px), 400)
            self.assertLessEqual(t.width_px * t.height_px, 100_000)


class TileTests(unittest.TestCase):
    def check_cover(self, plan, policy=EscalationPolicy()):
        """Tiles stay within the limits, keep the plan's DPI, and cover the crop pixel grid with no gap."""
        self.assertEqual(plan.action, "render")
        covered = set()
        for t in plan.tiles:
            self.assertLessEqual(t.width_px, policy.max_side_px)
            self.assertLessEqual(t.height_px, policy.max_side_px)
            self.assertLessEqual(t.width_px * t.height_px, policy.max_pixels)
            ox, oy = t.origin_px
            self.assertGreaterEqual(ox, 0)
            self.assertGreaterEqual(oy, 0)
            self.assertLessEqual(ox + t.width_px, plan.width_px)
            self.assertLessEqual(oy + t.height_px, plan.height_px)
            for x in (ox, ox + t.width_px - 1):
                for y in (oy, oy + t.height_px - 1):
                    covered.add((x, y))
        xs = sorted({x for x, _ in covered})
        ys = sorted({y for _, y in covered})
        self.assertEqual(xs[0], 0)
        self.assertEqual(ys[0], 0)
        self.assertEqual(xs[-1], plan.width_px - 1)
        self.assertEqual(ys[-1], plan.height_px - 1)
        # no gap along either axis: each tile starts at or before the previous column/row ends
        for axis in (0, 1):
            spans = sorted({(t.origin_px[axis], t.origin_px[axis] + (t.width_px, t.height_px)[axis]) for t in plan.tiles})
            reach = 0
            for start, end in spans:
                self.assertLessEqual(start, reach)
                reach = max(reach, end)
            self.assertEqual(reach, (plan.width_px, plan.height_px)[axis])

    def test_an_over_limit_crop_is_tiled_not_downscaled(self):
        p = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=(2000.0, 3000.0))
        self.assertGreater(p.width_px * p.height_px, MAX_PIXELS)
        self.assertEqual(p.dpi, 150)  # the DPI is kept; detail is never dropped
        self.assertGreater(len(p.tiles), 1)
        self.check_cover(p)

    def test_random_crops_are_covered_exactly(self):
        rng = random.Random(11)
        pol = EscalationPolicy(max_tiles=64)
        for _ in range(60):
            page = (rng.uniform(300, 3000), rng.uniform(300, 3000))
            x0, y0 = rng.uniform(0, 0.4), rng.uniform(0, 0.4)
            box = (x0, y0, rng.uniform(x0 + 0.2, 1.0), rng.uniform(y0 + 0.2, 1.0))
            p = plan_next([], "none", box=box, page_size_pt=page, policy=pol)
            if p.action == "render" and p.tiles:
                self.check_cover(p, pol)

    def test_inner_seams_overlap_by_the_configured_border_and_outer_edges_do_not(self):
        pol = EscalationPolicy(max_pixels=100_000, max_side_px=400, tile_overlap_px=8)
        p = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=(300.0, 300.0), policy=pol)
        self.assertEqual(min(t.origin_px[0] for t in p.tiles), 0)
        self.assertEqual(min(t.origin_px[1] for t in p.tiles), 0)
        columns = sorted({t.origin_px[0] for t in p.tiles})
        first = next(t for t in p.tiles if t.origin_px[0] == columns[0])
        self.assertEqual(first.origin_px[0] + first.width_px - columns[1], 16)  # 2 * overlap shared

    def test_escalating_to_300_dpi_can_require_tiles(self):
        page = (800.0, 1000.0)
        first = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=page)
        self.assertEqual(first.tiles, ())
        p = plan_next([Attempt(150, (0, 0, 1, 1), "default", first.width_px * first.height_px)], "blurry",
                      box=(0, 0, 1, 1), page_size_pt=page)
        self.assertEqual((p.action, p.dpi), ("render", 300))
        self.assertGreater(len(p.tiles), 1)
        self.check_cover(p)

    def test_too_many_tiles_or_budget_goes_to_review(self):
        pol = EscalationPolicy(max_tiles=2)
        p = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=(2000.0, 3000.0), policy=pol)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("pixel_limit_exceeded",)))
        pol = EscalationPolicy(pixel_budget=MAX_PIXELS)
        p = plan_next([], "none", box=(0, 0, 1, 1), page_size_pt=(2000.0, 3000.0), policy=pol)
        self.assertEqual(p.reasons, ("pixel_budget_exceeded",))  # the tiles together exceed the cost bound


class ValidationTests(ErrorCode, unittest.TestCase):
    def test_bad_inputs(self):
        for box in ((0, 0, 1), [0, 0, 1, 1], (0.5, 0.5, 0.5, 0.9), (0.6, 0, 0.5, 1), (-0.1, 0, 1, 1),
                    (0, 0, 1.1, 1), (0, 0, float("nan"), 1), (0, 0, True, 1), None):
            with self.subTest(box=box):
                self.assertCode("invalid_box", plan_next, [], "none", box=box, page_size_pt=PAGE)
        for page in ((0.0, 10.0), (10.0,), [10.0, 10.0], (float("inf"), 5.0), (True, 5.0), None):
            with self.subTest(page=page):
                self.assertCode("invalid_page_size", plan_next, [], "none", box=BOX, page_size_pt=page)
        self.assertCode("invalid_weakness", plan_next, [], "sharp", box=BOX, page_size_pt=PAGE)
        self.assertCode("invalid_history", plan_next, ["x"], "blurry", box=BOX, page_size_pt=PAGE)
        self.assertCode("invalid_policy", plan_next, [], "none", box=BOX, page_size_pt=PAGE, policy="x")
        self.assertCode("invalid_profile", plan_next, [], "none", box=BOX, page_size_pt=PAGE, default_profile="")


class PdfiumIntegrationRegressionTests(unittest.TestCase):
    """Meaningful real PDFium integration regressions for exact rounding and tiling."""

    def _make_pdf(self, width_pt: float, height_pt: float, rotation: int = 0) -> tuple[bytes, tuple[float, float]]:
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(width_pt, height_pt)
        if rotation:
            page.set_rotation(rotation)
        buf = io.BytesIO()
        pdf.save(buf)
        pdf.close()
        data = buf.getvalue()
        doc = pdfium.PdfDocument(data)
        size = doc[0].get_size()
        doc.close()
        return data, size

    def test_exact_fullpage_rounding_pdfium(self):
        # Host reproduced: real blank 301x301pt PDF at 150 DPI -> 628x628
        data, page_size = self._make_pdf(301.0, 301.0)
        p = plan_next([], "none", box=(0.0, 0.0, 1.0, 1.0), page_size_pt=page_size)
        asset = render_region(data, 0, region=(0.0, 0.0, 1.0, 1.0), dpi=150)
        self.assertEqual((p.width_px, p.height_px), (628, 628))
        self.assertEqual((asset.width_px, asset.height_px), (628, 628))
        self.assertEqual(p.tiles, ())

    def test_tiled_side_and_pixel_boundaries_pdfium(self):
        # Host reproduced: blank 1966.2x300pt PDF -> PDFium page_size (1966.199951171875, 300)
        # Plans 4097x625; width 4097 > 4096 so tiling is required.
        data, page_size = self._make_pdf(1966.2, 300.0)
        p = plan_next([], "none", box=(0.0, 0.0, 1.0, 1.0), page_size_pt=page_size)
        self.assertEqual((p.width_px, p.height_px), (4097, 625))
        self.assertGreaterEqual(len(p.tiles), 2)

        # Every tile must fit render limits and match rendered pixels exactly
        for idx, t in enumerate(p.tiles):
            asset = render_region(data, 0, region=t.box, dpi=p.dpi)
            self.assertEqual(
                (asset.width_px, asset.height_px),
                (t.width_px, t.height_px),
                f"Tile {idx} dimension mismatch",
            )
            self.assertLessEqual(t.width_px, MAX_SIDE_PX)
            self.assertLessEqual(t.height_px, MAX_SIDE_PX)
            self.assertLessEqual(t.width_px * t.height_px, MAX_PIXELS)

        # Pixel budget accounting must include all actual tiles
        tile_pixels = sum(t.width_px * t.height_px for t in p.tiles)
        pol_tight = EscalationPolicy(pixel_budget=16_000_000)
        p_tight = plan_next(
            [Attempt(150, (0.0, 0.0, 1.0, 1.0), "default", 16_000_000 - tile_pixels + 1)],
            "blurry",
            box=(0.0, 0.0, 1.0, 1.0),
            page_size_pt=page_size,
            policy=pol_tight,
        )
        self.assertEqual((p_tight.action, p_tight.reasons), ("needs_review", ("pixel_budget_exceeded",)))

    def test_exact_nonzero_crop_rounding_pdfium(self):
        data, page_size = self._make_pdf(612.0, 792.0)
        box = (0.1, 0.1, 0.5, 0.5)

        p150 = plan_next([], "none", box=box, page_size_pt=page_size)
        asset150 = render_region(data, 0, region=box, dpi=150)
        self.assertEqual((p150.width_px, p150.height_px), (asset150.width_px, asset150.height_px))
        self.assertEqual((p150.width_px, p150.height_px), (509, 659))

        p300 = plan_next([Attempt(150, box, "default", 509 * 659)], "blurry", box=box, page_size_pt=page_size)
        asset300 = render_region(data, 0, region=box, dpi=300)
        self.assertEqual((p300.width_px, p300.height_px), (asset300.width_px, asset300.height_px))
        self.assertEqual((p300.width_px, p300.height_px), (1019, 1319))

    def test_rotated_cropbox_quantization_pdfium(self):
        data, page_size = self._make_pdf(301.0, 401.0, rotation=90)
        p = plan_next([], "none", box=(0.0, 0.0, 1.0, 1.0), page_size_pt=page_size)
        asset = render_region(data, 0, region=(0.0, 0.0, 1.0, 1.0), dpi=150)
        self.assertEqual((p.width_px, p.height_px), (836, 628))
        self.assertEqual((asset.width_px, asset.height_px), (836, 628))


class HistoryAndOverflowRegressionTests(ErrorCode, unittest.TestCase):
    """Validation regressions for invalid history, schema, and page size overflow."""

    def test_oversized_integers_are_sanitized_or_budget_gated(self):
        huge = 10 ** 400
        self.assertCode("invalid_page_size", plan_next, [], "none", box=BOX, page_size_pt=(huge, 300))
        self.assertCode("invalid_box", plan_next, [], "none", box=(0, 0, huge, 1), page_size_pt=PAGE)
        p = plan_next([Attempt(150, BOX, "default", huge)], "blurry", box=BOX, page_size_pt=PAGE)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("pixel_budget_exceeded",)))

    def test_invalid_attempt_pixels(self):
        # Negative pixels
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "default", -100000000)], "blurry",
                        box=BOX, page_size_pt=PAGE)
        # NaN pixels
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "default", float("nan"))], "blurry",  # type: ignore[arg-type]
                        box=BOX, page_size_pt=PAGE)
        # Zero / bool / non-int pixels
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "default", 0)], "blurry",
                        box=BOX, page_size_pt=PAGE)
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "default", True)], "blurry",  # type: ignore[arg-type]
                        box=BOX, page_size_pt=PAGE)

        # Valid pixels at budget bound goes to needs_review, not uncaught or render
        pol = EscalationPolicy(pixel_budget=16_000_000)
        p = plan_next([Attempt(150, BOX, "default", 16_000_000)], "blurry", box=BOX, page_size_pt=PAGE, policy=pol)
        self.assertEqual((p.action, p.reasons), ("needs_review", ("pixel_budget_exceeded",)))

    def test_invalid_attempt_dpi_out_of_policy(self):
        # Attempt with 999 DPI
        self.assertCode("invalid_history", plan_next, [Attempt(999, BOX, "default", 100)], "missing_labels",
                        box=BOX, page_size_pt=PAGE)
        # Attempt with DPI not in policy (e.g. 72 when policy is 150/300)
        self.assertCode("invalid_history", plan_next, [Attempt(72, BOX, "default", 100)], "blurry",
                        box=BOX, page_size_pt=PAGE)
        # Bool DPI
        self.assertCode("invalid_history", plan_next, [Attempt(True, BOX, "default", 100)], "blurry",  # type: ignore[arg-type]
                        box=BOX, page_size_pt=PAGE)

    def test_invalid_attempt_box_and_profile(self):
        # Invalid box coordinates
        self.assertCode("invalid_history", plan_next, [Attempt(150, (0, 0, 1), "default", 100)], "blurry",  # type: ignore[arg-type]
                        box=BOX, page_size_pt=PAGE)
        self.assertCode("invalid_history", plan_next, [Attempt(150, (0, 0, 0.5, float("nan")), "default", 100)], "blurry",
                        box=BOX, page_size_pt=PAGE)
        # Empty or oversized profile
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "", 100)], "blurry",
                        box=BOX, page_size_pt=PAGE)
        self.assertCode("invalid_history", plan_next, [Attempt(150, BOX, "x" * 65, 100)], "blurry",
                        box=BOX, page_size_pt=PAGE)

    def test_impossible_bounded_history_rejected(self):
        # History with de-escalating DPI (300 -> 150)
        self.assertCode("invalid_history", plan_next,
                        [Attempt(300, BOX, "default", 100), Attempt(150, BOX, "default", 100)],
                        "blurry", box=BOX, page_size_pt=PAGE)
        # History longer than max_attempts
        pol = EscalationPolicy(max_attempts=3)
        long_history = [Attempt(150, BOX, "default", 100)] * 4
        self.assertCode("invalid_history", plan_next, long_history, "blurry", box=BOX, page_size_pt=PAGE, policy=pol)

    def test_gigantic_page_dimensions_overflow_rejected(self):
        # Finite gigantic dimensions that would overflow floating-point scaling or int conversions
        for huge_page in ((1e308, 792.0), (612.0, 1e308), (1e300, 1e300), (1e15, 100.0)):
            with self.subTest(page=huge_page):
                self.assertCode("invalid_page_size", plan_next, [], "none", box=BOX, page_size_pt=huge_page)


if __name__ == "__main__":
    unittest.main()
