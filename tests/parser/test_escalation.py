"""Limited DPI/crop escalation planning (SPEC 19.3 item 8)."""
from __future__ import annotations

import random
import unittest

from lab_parser.escalation import (
    Attempt, EscalationError, EscalationPolicy, WEAKNESSES, plan_next,
)
from lab_parser.render import MAX_PIXELS, MAX_SIDE_PX

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
        # render.render_region: round((x1 - x0) * page_w_pt * dpi / 72)
        self.assertEqual((p.width_px, p.height_px), (round(0.4 * 612 * 150 / 72), round(0.4 * 792 * 150 / 72)))
        self.assertEqual(p.tiles, ())
        self.assertEqual(p.policy_version, "dpi-escalation-1")

    def test_weakness_is_ignored_for_the_first_render(self):
        self.assertEqual(plan_next([], "blurry", box=BOX, page_size_pt=PAGE).dpi, 150)

    def test_a_tiny_box_still_renders_one_pixel_at_least(self):
        p = plan_next([], "none", box=(0.5, 0.5, 0.5001, 0.5001), page_size_pt=(10.0, 10.0))
        self.assertEqual((p.width_px, p.height_px), (1, 1))


class DpiEscalationTests(ErrorCode, unittest.TestCase):
    def first(self):
        p = plan_next([], "none", box=BOX, page_size_pt=PAGE)
        return [attempt_of(p)]

    def test_too_small_or_blurry_escalates_once_to_300_with_the_same_box_and_profile(self):
        for weakness in ("too_small", "blurry"):
            with self.subTest(weakness=weakness):
                p = plan_next(self.first(), weakness, box=BOX, page_size_pt=PAGE)
                self.assertEqual((p.action, p.attempt_no, p.dpi, p.box, p.profile), ("render", 2, 300, BOX, "default"))
                self.assertEqual((p.width_px, p.height_px), (round(0.4 * 612 * 300 / 72), round(0.4 * 792 * 300 / 72)))

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


if __name__ == "__main__":
    unittest.main()
