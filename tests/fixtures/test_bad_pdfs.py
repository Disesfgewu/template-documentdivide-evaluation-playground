"""F05: each registered bad input really has the property its expected
outcome is about (checked with pypdf in strict mode, independently of #7)."""
import io
import json
import unittest

from pypdf import PdfReader
from pypdf.errors import EmptyFileError, FileNotDecryptedError, PdfReadError

from fixtures import registry_check as rc

ENTRY = rc.load()["fixtures"][4]
ITEMS = {it["id"]: it for it in ENTRY["items"]}
EXPECTED = json.loads((rc.ROOT / ENTRY["expected"]).read_text())["items"]


def data(case):
    return rc.item_bytes(ITEMS[f"F05.{case}"])


def open_strict(case):
    return PdfReader(io.BytesIO(data(case)), strict=True)


class F05Tests(unittest.TestCase):
    def test_every_item_has_an_outcome(self):
        self.assertEqual(set(ITEMS), set(EXPECTED))

    def test_unreadable_inputs(self):
        with self.assertRaises(EmptyFileError):
            open_strict("empty")
        for case in ("fake_text", "fake_html", "truncated"):
            with self.subTest(case), self.assertRaises(PdfReadError):
                open_strict(case)
        self.assertFalse(data("fake_html").startswith(b"%PDF"))

    def test_bad_xref_only_opens_through_repair(self):
        with self.assertLogs("pypdf", level="WARNING") as logs:
            reader = open_strict("bad_xref")
            self.assertEqual(len(reader.pages), EXPECTED["F05.bad_xref"]["page_count"])
        self.assertTrue(any("repaired" in m for m in logs.output))

    def test_encrypted_needs_password(self):
        reader = open_strict("encrypted")
        self.assertTrue(reader.is_encrypted)
        with self.assertRaises(FileNotDecryptedError):
            len(reader.pages)
        reader = open_strict("encrypted")
        self.assertTrue(reader.decrypt("lab-user"))
        self.assertEqual(len(reader.pages), EXPECTED["F05.encrypted"]["page_count_after_decrypt"])

    def test_many_pages_is_over_the_generator_cap(self):
        self.assertEqual(len(open_strict("many_pages").pages), 2001)

    def test_injection_text_is_present_as_plain_page_text(self):
        text = open_strict("injection").pages[0].extract_text()
        for line in EXPECTED["F05.injection"]["contains"]:
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
