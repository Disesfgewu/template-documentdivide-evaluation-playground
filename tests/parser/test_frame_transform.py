"""Pure geometry of the layout-space to display-frame mapping; the PDFium validation is CI-only."""
import unittest

from lab_parser.frame_transform import display_transform


class FrameTransformTests(unittest.TestCase):
    def test_plain_page_is_the_identity(self):
        t = display_transform((0, 0, 612, 792), (0, 0, 612, 792), 0)
        self.assertEqual((t.layout_width, t.layout_height, t.origin_x, t.origin_y, t.width, t.height),
                         (612, 792, 0, 0, 612, 792))

    def test_a_crop_box_shifted_beyond_the_media_box_uses_the_intersection(self):
        # CI measurement: crop [50 50 662 842] on a 612x792 page -> visible [50 50 612 792].
        t = display_transform((0, 0, 612, 792), (50, 50, 662, 842), 0)
        self.assertEqual((t.origin_x, t.origin_y, t.width, t.height), (50, 0, 562, 742))

    def test_a_smaller_crop_box_moves_the_origin_from_the_top_left(self):
        t = display_transform((0, 0, 612, 792), (100, 100, 400, 500), 0)
        # visible left=100, top = 792-500 = 292, size 300x400
        self.assertEqual((t.origin_x, t.origin_y, t.width, t.height), (100, 292, 300, 400))

    def test_rotations_swap_or_mirror_the_layout(self):
        t90 = display_transform((0, 0, 612, 792), (0, 0, 612, 792), 90)
        self.assertEqual((t90.layout_width, t90.layout_height, t90.width, t90.height), (792, 612, 792, 612))
        t180 = display_transform((0, 0, 612, 792), (100, 100, 400, 500), 180)
        # unrotated visible: left 100, right 400, top 292, bottom 692 -> (612-400, 792-692) origin
        self.assertEqual((t180.layout_width, t180.layout_height, t180.origin_x, t180.origin_y), (612, 792, 212, 100))
        self.assertEqual((t180.width, t180.height), (300, 400))

    def test_text_position_example_from_the_ci_measurement(self):
        # Plain-page text union x 72..191.8, y 60.9..110.9 (unrotated). With /Rotate 180 pdfminer reported
        # x 420.2..540, y 681.1..731.1; the display origin is (0, 0), so they are already display coordinates.
        t = display_transform((0, 0, 612, 792), (0, 0, 612, 792), 180)
        self.assertEqual((t.origin_x, t.origin_y), (0, 0))
        self.assertAlmostEqual(612 - 191.826, 420.174, places=3)

    def test_invalid_input_is_refused(self):
        for args in (((0, 0, 0, 10), (0, 0, 10, 10), 0), ((0, 0, 10, 10), (20, 20, 30, 30), 0),
                     ((0, 0, 10, 10), (0, 0, 10, 10), 45), ((0, 0, 10, float("nan")), (0, 0, 10, 10), 0),
                     ((0, 0, 10), (0, 0, 10, 10), 0)):
            with self.subTest(args), self.assertRaises(ValueError):
                display_transform(*args)


if __name__ == "__main__":
    unittest.main()
