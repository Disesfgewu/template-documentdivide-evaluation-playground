"""Unit tests for PDF page geometry extraction and normalization (SPEC 4.1 coordinates, T05, 19.2 L0, 19.3.8 limits)."""

from __future__ import annotations

import dataclasses
import unittest

from pypdf.errors import PdfReadError

from lab_parser.pages import InvalidPageLabels, PageInfo, extract_pages
from tests.parser.pdfgen import build_pdf

import io

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject


def with_label_tree(tree, pages=3) -> bytes:
    """A PDF whose /PageLabels is exactly ``tree`` (built from plain Python:
    dict -> DictionaryObject, list -> ArrayObject, "/X" -> name, int, str)."""
    def obj(v):
        if isinstance(v, dict):
            d = DictionaryObject()
            for k, x in v.items():
                d[NameObject(k)] = obj(x)
            return d
        if isinstance(v, list):
            return ArrayObject(obj(x) for x in v)
        if isinstance(v, int):
            return NumberObject(v)
        if isinstance(v, str) and v.startswith("/"):
            return NameObject(v)
        return TextStringObject(v)
    reader = PdfReader(io.BytesIO(build_pdf([{"mediabox": (0, 0, 200, 100)}] * pages)))
    writer = PdfWriter(clone_from=reader)
    writer.root_object[NameObject("/PageLabels")] = obj(tree)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def labels(data: bytes) -> list:
    return [p.page_label for p in extract_pages(data)]


class TestPages(unittest.TestCase):
    def test_three_pages_geometry_and_frozen(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 612, 792)},
            {"mediabox": (0, 0, 600, 400), "cropbox": (50, 20, 550, 380), "rotate": 90},
            {"mediabox": (0, 0, 300, 200), "rotate": -90},
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 3)
        self.assertEqual([p.page_index for p in pages], [0, 1, 2])
        self.assertEqual([p.rotation for p in pages], [0, 90, 270])

        p0 = pages[0]
        self.assertEqual(p0.display_width_pt, 612.0)
        self.assertEqual(p0.display_height_pt, 792.0)

        p1 = pages[1]
        self.assertEqual(p1.mediabox, (0.0, 0.0, 600.0, 400.0))
        self.assertEqual(p1.cropbox, (50.0, 20.0, 550.0, 380.0))
        self.assertEqual(p1.visible_box, (50.0, 20.0, 550.0, 380.0))
        self.assertEqual(p1.display_width_pt, 360.0)
        self.assertEqual(p1.display_height_pt, 500.0)

        p2 = pages[2]
        self.assertEqual(p2.display_width_pt, 200.0)
        self.assertEqual(p2.display_height_pt, 300.0)

        for p in pages:
            self.assertIsInstance(p, PageInfo)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                p.rotation = 0  # type: ignore[misc]

    def test_cropbox_larger_than_mediabox(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 300, 200), "cropbox": (-50, -50, 250, 150)}
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 1)
        p = pages[0]
        self.assertEqual(p.visible_box, (0.0, 0.0, 250.0, 150.0))
        self.assertEqual(p.display_width_pt, 250.0)
        self.assertEqual(p.display_height_pt, 150.0)

    def test_regression_no_page_labels(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 100, 100)},
            {"mediabox": (0, 0, 100, 100)},
            {"mediabox": (0, 0, 100, 100)},
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 3)
        for p in pages:
            self.assertIsNone(p.page_label)

    def test_page_labels(self) -> None:
        pdf_bytes = build_pdf(
            [
                {"mediabox": (0, 0, 100, 100)},
                {"mediabox": (0, 0, 100, 100)},
                {"mediabox": (0, 0, 100, 100)},
            ],
            labels=[
                {"from": 0, "to": 1, "style": "/r"},
                {"from": 2, "to": 2, "style": "/D", "prefix": "A-", "start": 1},
            ],
        )
        pages = extract_pages(pdf_bytes)

        self.assertEqual([p.page_label for p in pages], ["i", "ii", "A-1"])

    def test_pm_repro_empty_label_tree_rejected(self) -> None:
        """PM-REVIEW on #36 (6a90e20): an empty /PageLabels dictionary made
        pypdf fall back to page_label '1'. It is now a controlled error."""
        with self.assertRaises(InvalidPageLabels) as ctx:
            extract_pages(with_label_tree({}, pages=1))
        self.assertEqual(str(ctx.exception), "invalid_page_labels: node needs exactly one of /Nums or /Kids")

    def test_malformed_label_trees_rejected(self) -> None:
        bad = {
            "empty Nums": {"/Nums": []},
            "odd Nums": {"/Nums": [0]},
            "no entry for page 0": {"/Nums": [1, {"/S": "/D"}]},
            "keys not increasing": {"/Nums": [0, {"/S": "/D"}, 2, {"/S": "/r"}, 1, {"/S": "/D"}]},
            "duplicate key": {"/Nums": [0, {"/S": "/D"}, 0, {"/S": "/r"}]},
            "negative key": {"/Nums": [-1, {"/S": "/D"}]},
            "value not a dict": {"/Nums": [0, 5]},
            "unknown style": {"/Nums": [0, {"/S": "/X"}]},
            "bad start": {"/Nums": [0, {"/S": "/D", "/St": 0}]},
            "both Nums and Kids": {"/Nums": [0, {}], "/Kids": [{"/Nums": [0, {}]}]},
            "empty Kids": {"/Kids": []},
            "kid not a dict": {"/Kids": [3]},
            "tree not a dict": [0, {"/S": "/D"}],
        }
        for label, tree in bad.items():
            with self.subTest(label):
                with self.assertRaises(InvalidPageLabels):
                    extract_pages(with_label_tree(tree))

    def test_genuine_label_styles(self) -> None:
        tree = {"/Nums": [0, {"/S": "/r"}, 2, {"/S": "/D", "/P": "A-"}, 3, {"/P": "Cover"},
                          4, {"/S": "/R", "/St": 4}, 5, {"/S": "/A", "/St": 27}, 6, {"/S": "/a", "/St": 2}]}
        self.assertEqual(labels(with_label_tree(tree, pages=7)), ["i", "ii", "A-1", "Cover", "IV", "AA", "b"])
        # Labels split across /Kids nodes (an intermediate number tree).
        kids = {"/Kids": [{"/Limits": [0, 0], "/Nums": [0, {"/S": "/r"}]},
                          {"/Limits": [1, 1], "/Nums": [1, {"/S": "/D", "/St": 10}]}]}
        self.assertEqual(labels(with_label_tree(kids)), ["i", "10", "11"])

    def test_invalid_rotation_45(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 100, 100), "rotate": 45}
        ])
        with self.assertRaises(ValueError):
            extract_pages(pdf_bytes)

    def test_garbage_bytes(self) -> None:
        with self.assertRaises(PdfReadError):
            extract_pages(b"not a pdf")


if __name__ == "__main__":
    unittest.main()
