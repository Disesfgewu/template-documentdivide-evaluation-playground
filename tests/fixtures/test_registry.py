import copy
import io
import json
import unittest

from pypdf import PdfReader

from fixtures import registry_check as rc
from fixtures.generators import synthetic_pdf

DOC = rc.load()


def errs(mutate, reproduce=False):
    doc = copy.deepcopy(DOC)
    mutate(doc)
    return rc.check(doc, reproduce=reproduce)


def f01(doc):
    return doc["fixtures"][0]


class RegistryTests(unittest.TestCase):
    def test_registry_valid_and_reproducible(self):
        self.assertEqual(rc.check(DOC, reproduce=True), [])

    def test_hash_mismatch_detected(self):
        got = errs(lambda d: f01(d)["items"][0].update(sha256="0" * 64), reproduce=True)
        self.assertEqual(got, ["F01.pages_1: sha256 mismatch (not reproducible)"])

    def test_model_golden_forbidden(self):
        got = errs(lambda d: f01(d)["golden"].update(origin="model"))
        self.assertTrue(any("never model output" in e for e in got))

    def test_available_requires_human_review(self):
        got = errs(lambda d: f01(d).update(status="available"))
        self.assertIn("F01: available requires a human review record", got)

    def test_human_review_gate(self):
        """PM-REVIEW 6676133319: a flag-only, untraceable, impossible-date or
        stale signoff must not promote a fixture to available (T05/T14)."""
        def promote(review):
            def mutate(d):
                f01(d).update(status="available", missing=[])
                f01(d)["golden"]["review"] = review
            return mutate
        digest = rc.fixture_digest(f01(DOC)["items"])
        good = {"human": True, "by": "Disesfgewu", "date": "2026-11-05",
                "method": "page counts re-read with pypdf and compared with the SPEC T14 table",
                "signoff": f"https://github.com/{rc.REPO}/issues/31#issuecomment-6676139956",
                "fixture_digest": digest}
        self.assertEqual(errs(promote(good), reproduce=False), [])
        bad = {
            "PM repro: agent by, impossible date": {"human": True, "by": "coding-agent", "date": "2026-99-99"},
            "flag only": {"human": True, "by": "Disesfgewu", "date": "2026-11-05", "method": "looked"},
            "invalid calendar date": dict(good, date="2026-02-30"),
            "future date": dict(good, date="2999-01-01"),
            "automation identity": dict(good, by="coding-agent"),
            "bot identity": dict(good, by="github-actions[bot]"),
            "signoff outside repo": dict(good, signoff="https://example.com/ok"),
            "no method": dict(good, method=" "),
            "stale review": dict(good, fixture_digest=rc.fixture_digest(f01(DOC)["items"][1:])),
        }
        for label, review in bad.items():
            with self.subTest(label):
                got = errs(promote(review), reproduce=False)
                self.assertTrue(got, label)
        stale = errs(promote(bad["stale review"]), reproduce=False)
        self.assertIn("F01: human review is stale (fixture_digest does not match the current items)", stale)
        # Changing a reviewed item after signoff also makes the review stale.
        def changed(d):
            promote(good)(d)
            f01(d)["items"][0]["sha256"] = "1" * 64
        self.assertTrue(any("stale" in e for e in errs(changed, reproduce=False)))
        # Unreviewed fixtures stay partial and still pass.
        for e in DOC["fixtures"]:
            with self.subTest(fid=e["id"]):
                if e["status"] != "planned":
                    self.assertEqual(e["status"], "partial")
                    self.assertIs(e["golden"]["review"]["human"], False)

    def test_generator_outside_package_rejected(self):
        got = errs(lambda d: f01(d)["items"][0].update(generator="os"), reproduce=True)
        self.assertTrue(any("outside fixtures.generators" in e for e in got))

    def test_malformed_entries_reported_not_crashing(self):
        cases = {
            "expected outside fixtures": lambda d: f01(d).update(expected="../package.json"),
            "expected not a string": lambda d: f01(d).update(expected=123),
            "args not a list": lambda d: f01(d)["items"][0].update(args=None),
            "generator __init__": lambda d: f01(d)["items"][0].update(generator="fixtures.generators.__init__"),
            "file escapes": lambda d: f01(d)["items"][0].update(generator=None) or f01(d)["items"][0].pop("generator")
                            or f01(d)["items"][0].update(file="../../etc/passwd"),
            "entry not a mapping": lambda d: d["fixtures"].insert(1, "junk"),
            "bad review date": lambda d: f01(d)["golden"]["review"].update(date="yesterday"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assertTrue(errs(mutate, reproduce=True))

    def test_all_ids_required(self):
        self.assertTrue(errs(lambda d: d["fixtures"].pop()))


class F01Tests(unittest.TestCase):
    """T14 (U/I part): page counts read back from the generated PDFs by an
    independent parser drive the targets, compared with the SPEC table."""

    def test_t14_from_generated_pdfs(self):
        from lab_contracts.benchmark import target_counts
        entry = DOC["fixtures"][0]
        expected = json.loads((rc.ROOT / entry["expected"]).read_text())["items"]
        for item in entry["items"]:
            with self.subTest(item["id"]):
                reader = PdfReader(io.BytesIO(rc.item_bytes(item)), strict=True)
                want = expected[item["id"]]
                self.assertEqual(len(reader.pages), want["page_count"])
                self.assertEqual(target_counts(len(reader.pages)), (want["n_target"], want["q_min"]))

    def test_every_registered_pdf_has_distinct_pages(self):
        # SPEC 17.1: a large PDF must not be one page copied N times.
        for item in DOC["fixtures"][0]["items"]:
            with self.subTest(item["id"]):
                reader = PdfReader(io.BytesIO(rc.item_bytes(item)), strict=True)
                texts = [p.extract_text() for p in reader.pages]
                self.assertEqual(len(set(texts)), len(texts))
        self.assertIn("page 21", PdfReader(io.BytesIO(synthetic_pdf.build(21))).pages[20].extract_text())

    def test_deterministic_and_bounded(self):
        self.assertEqual(synthetic_pdf.build(3), synthetic_pdf.build(3))
        for bad in (0, 2001, True, 3.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                synthetic_pdf.build(bad)


if __name__ == "__main__":
    unittest.main()
