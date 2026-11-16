"""Benign, listed reader notes are accepted AND recorded; anything else keeps its own rejection; a scan is flagged (Issues #204 and #203, PM-DECISION)."""
import unittest

from fixtures.generators import synthetic_pdf
from lab_parser.artifacts import Limits, inspect_pdf_isolated
from lab_parser.isolation import FLAG_CODES, inspect_geometry


def non_zero_indexed(pdf: bytes) -> bytes:
    """The PaperPort shape: the xref table starts at object 1 and has no free entry for object 0 (pypdf corrects the numbering, the bytes stay)."""
    i = pdf.find(b"\nxref\n") + len(b"\nxref\n")
    head, rest = pdf[:i], pdf[i:]
    header, _, entries = rest.partition(b"\n")
    count = int(header.split()[1])
    first_entry_len = len(b"0000000000 65535 f \n")
    assert entries.startswith(b"0000000000 65535 f")
    return head + f"1 {count - 1}\n".encode() + entries[first_entry_len:]


def image_only(pages: int) -> bytes:
    """A document with pages and no text at all: what a scan is for the text pipeline (blank pages: no text layer, no content)."""
    from tests.parser.pdfgen import build_pdf
    return build_pdf([{"mediabox": (0, 0, 595, 842)}] * pages)


class ListedNotesTests(unittest.TestCase):
    def setUp(self):
        self.pdf = synthetic_pdf.build(3)

    def test_a_non_zero_indexed_xref_is_accepted_flagged_and_the_pages_still_agree(self):
        data = non_zero_indexed(self.pdf)
        self.assertNotEqual(data, self.pdf)
        result, pages = inspect_pdf_isolated(data, Limits())
        self.assertEqual((result.outcome, result.page_count), ("flag", 3))
        self.assertIn("xref_not_zero_indexed", result.warnings)
        self.assertEqual(result.sha256, __import__("hashlib").sha256(data).hexdigest())        # the original bytes, never repaired
        self.assertEqual(len(pages), 3)

    def test_trailing_bytes_after_eof_are_accepted_and_flagged_when_nothing_else_is_said(self):
        data = self.pdf + b"%PaperPortPDFversion 11\n"
        result, _ = inspect_pdf_isolated(data, Limits())
        self.assertEqual((result.outcome, result.page_count), ("flag", 3))
        self.assertIn("trailing_bytes_after_eof", result.warnings)

    def test_trailing_whitespace_is_not_a_flag(self):
        result, _ = inspect_pdf_isolated(self.pdf + b"\n\r\n  \x00", Limits())
        self.assertNotIn("trailing_bytes_after_eof", result.warnings)

    def test_both_notes_together_are_both_recorded(self):
        result, _ = inspect_pdf_isolated(non_zero_indexed(self.pdf) + b"%tail\n", Limits())
        self.assertEqual(result.outcome, "flag")
        self.assertTrue({"xref_not_zero_indexed", "trailing_bytes_after_eof"} <= set(result.warnings))

    def test_every_flag_code_the_child_can_report_is_a_known_one(self):
        self.assertEqual(FLAG_CODES, {"xref_not_zero_indexed", "trailing_bytes_after_eof"})
        insp = inspect_geometry(non_zero_indexed(self.pdf) + b"x")
        self.assertTrue(set(insp.warnings) <= FLAG_CODES)


class RealRejectionsTests(unittest.TestCase):
    def setUp(self):
        self.pdf = synthetic_pdf.build(3)

    def test_a_truly_broken_xref_is_still_rejected_as_unreadable(self):
        data = self.pdf.replace(b"startxref\n", b"startxref\n9")                         # the offset points into nothing
        result, _ = inspect_pdf_isolated(data, Limits())
        self.assertEqual(result.outcome, "reject")
        self.assertIn(result.reason, ("corrupt_pdf", "pdf_parser_warning"))

    def test_a_truncated_file_is_unreadable(self):
        result, _ = inspect_pdf_isolated(self.pdf[: len(self.pdf) // 2], Limits())
        self.assertEqual((result.outcome, result.reason), ("reject", "corrupt_pdf"))

    def test_a_warning_that_is_not_on_the_list_is_its_own_rejection_not_corruption(self):
        # a readable file whose page tree gives pypdf a warning that is not the listed one: here an object-number mismatch in the xref offsets
        broken = self.pdf.replace(b"0000000015 00000 n", b"0000000016 00000 n", 1)
        result, _ = inspect_pdf_isolated(broken, Limits())
        self.assertEqual(result.outcome, "reject")
        self.assertIn(result.reason, ("pdf_parser_warning", "corrupt_pdf"))
        self.assertEqual(result.warnings, ())                                              # nothing is recorded for a rejection

    def test_a_page_count_disagreement_between_the_readers_is_still_rejected(self):
        data = self.pdf.replace(b"/Count 3", b"/Count 99")
        result, _ = inspect_pdf_isolated(data, Limits())
        self.assertEqual(result.outcome, "reject")


class NoTextLayerTests(unittest.TestCase):
    def test_a_document_with_no_text_at_all_is_accepted_and_flagged_no_text_layer(self):
        result, _ = inspect_pdf_isolated(image_only(5), Limits())
        self.assertEqual((result.outcome, result.page_count), ("flag", 5))
        self.assertIn("no_text_layer", result.warnings)

    def test_the_sample_is_bounded_and_spread(self):
        insp = inspect_geometry(image_only(120))
        self.assertEqual(insp.text_pages, (20, 0))


if __name__ == "__main__":
    unittest.main()
