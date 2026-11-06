import copy
import io
import json
import unittest

from pypdf import PdfReader

from fixtures import registry_check as rc
from fixtures.generators import synthetic_pdf

DOC = rc.load()


def errs(mutate, reproduce=False, verify=None):
    doc = copy.deepcopy(DOC)
    mutate(doc)
    return rc.check(doc, reproduce=reproduce, verify_signoff=verify)


def f01(doc):
    return doc["fixtures"][0]


EXPECTED = rc._expected_items("fixtures/F01/expected.json")
COMMENT_ID = 7000000001


def promote(review):
    def mutate(d):
        f01(d).update(status="available", missing=[])
        f01(d)["golden"]["review"] = review
    return mutate


def fake_fetch(body=None, permissions=None, **change):
    """Stand-in for gh api: only COMMENT_ID exists, on issue 31; repository
    permissions per login (default: Disesfgewu is admin, others none)."""
    permissions = {"Disesfgewu": "admin"} if permissions is None else permissions
    digest = rc.fixture_digest(f01(DOC)["items"], EXPECTED)
    record = {"issue_url": f"https://api.github.com/repos/{rc.REPO}/issues/31",
              "user": {"login": "Disesfgewu", "type": "User"}, "author_association": "OWNER",
              "body": rc.signoff_line("F01", digest) if body is None else body, **change}

    def fetch(path):
        if path == f"repos/{rc.REPO}/issues/comments/{COMMENT_ID}":
            return record
        if path.startswith(f"repos/{rc.REPO}/collaborators/") and path.endswith("/permission"):
            login = path.split("/")[-2]
            return {"permission": permissions.get(login, "none")}
        raise RuntimeError(f"HTTP 404: {path}")
    return fetch


def signed_off(body=None, by="Disesfgewu", permissions=None, **change):
    review = {"human": True, "by": by, "date": "2026-11-05",
              "method": "page counts re-read with pypdf and compared with the SPEC T14 table",
              "signoff": f"https://github.com/{rc.REPO}/issues/31#issuecomment-{COMMENT_ID}",
              "fixture_digest": rc.fixture_digest(f01(DOC)["items"], EXPECTED)}
    return review, rc.github_signoff_verifier(fake_fetch(body, permissions, **change))


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
        good, verify = signed_off()
        self.assertEqual(errs(promote(good), verify=verify), [])
        bad = {
            "PM repro: agent by, impossible date": {"human": True, "by": "coding-agent", "date": "2026-99-99"},
            "flag only": {"human": True, "by": "Disesfgewu", "date": "2026-11-05", "method": "looked"},
            "invalid calendar date": dict(good, date="2026-02-30"),
            "future date": dict(good, date="2999-01-01"),
            "automation identity": dict(good, by="coding-agent"),
            "bot identity": dict(good, by="github-actions[bot]"),
            "signoff outside repo": dict(good, signoff="https://example.com/ok"),
            "no method": dict(good, method=" "),
            "stale review": dict(good, fixture_digest=rc.fixture_digest(f01(DOC)["items"][1:], EXPECTED)),
        }
        for label, review in bad.items():
            with self.subTest(label):
                got = errs(promote(review), verify=verify)
                self.assertTrue(got, label)
        stale = errs(promote(bad["stale review"]), verify=verify)
        self.assertIn("F01: human review is stale (fixture_digest does not match the current items/expected)", stale)
        # Changing a reviewed item after signoff also makes the review stale.
        def changed(d):
            promote(good)(d)
            f01(d)["items"][0]["sha256"] = "1" * 64
        self.assertTrue(any("stale" in e for e in errs(changed, verify=verify)))
        # Unreviewed fixtures stay partial and still pass.
        for e in DOC["fixtures"]:
            with self.subTest(fid=e["id"]):
                if e["status"] != "planned":
                    self.assertEqual(e["status"], "partial")
                    self.assertIs(e["golden"]["review"]["human"], False)

    def test_signoff_must_be_resolved(self):
        """PM-REVIEW on #34 (c7aaed4): a well-formed review pointing at
        issuecomment-0 was accepted offline. The record is now resolved and
        must be the reviewer's explicit signoff of this fixture and digest."""
        good, verify = signed_off()
        pm_repro = dict(good, signoff=f"https://github.com/{rc.REPO}/issues/31#issuecomment-0")
        # Offline: never accepted, however well-formed.
        self.assertIn("F01: human review requires a resolved signoff (run with --verify-signoff)",
                      errs(promote(good)))
        self.assertIn("F01: human review requires a resolved signoff (run with --verify-signoff)",
                      errs(promote(pm_repro)))
        # Nonexistent record.
        got = errs(promote(pm_repro), verify=verify)
        self.assertTrue(any("signoff record could not be resolved" in e for e in got), got)
        digest = good["fixture_digest"]
        unrelated = {
            "PM change request, no signoff line": ("PM-REVIEW: REQUEST_CHANGES ...", {}, "does not contain"),
            "signoff of another fixture": (rc.signoff_line("F02", digest), {}, "does not contain"),
            "signoff of an older digest": (rc.signoff_line("F01", "0" * 64), {}, "does not contain"),
            "line only quoted in prose": (f"please post `{rc.signoff_line('F01', digest)}`", {}, "does not contain"),
            "another author": (rc.signoff_line("F01", digest), {"user": {"login": "someone", "type": "User"}},
                               "is not reviewer"),
            "bot author": (rc.signoff_line("F01", digest), {"user": {"login": "Disesfgewu", "type": "Bot"}},
                           "is not reviewer"),
            "comment on another issue": (rc.signoff_line("F01", digest),
                                         {"issue_url": f"https://api.github.com/repos/{rc.REPO}/issues/30"},
                                         "is not on issues/31"),
        }
        for label, (body, change, msg) in unrelated.items():
            with self.subTest(label):
                _, v = signed_off(body=body, **change)
                got = errs(promote(good), verify=v)
                self.assertTrue(any(msg in e for e in got), got)
        # PM-REVIEW on #34 (305f766): an outsider's exact signoff line, with
        # by=outsider, promoted F01. The signer must be trusted by GitHub.
        outsider = {"user": {"login": "outsider", "type": "User"}, "author_association": "NONE"}
        review, v = signed_off(by="outsider", **outsider)
        got = errs(promote(review), verify=v)
        self.assertIn("F01: signoff author 'outsider' is not a repository owner/member/collaborator "
                      "(author_association='NONE')", got)
        # A collaborator with read-only access is not trusted either.
        reader = {"user": {"login": "reader1", "type": "User"}, "author_association": "COLLABORATOR"}
        review, v = signed_off(by="reader1", permissions={"reader1": "read"}, **reader)
        self.assertIn("F01: signoff author 'reader1' lacks write access (permission='read')",
                      errs(promote(review), verify=v))
        # Authorized signers: the owner, and a write collaborator.
        writer = {"user": {"login": "writer1", "type": "User"}, "author_association": "COLLABORATOR"}
        review, v = signed_off(by="writer1", permissions={"writer1": "write"}, **writer)
        self.assertEqual(errs(promote(review), verify=v), [])
        self.assertEqual(errs(promote(good), verify=verify), [])
        # Changing an expected/golden value after signoff makes it stale.
        def changed_expected(d):
            promote(good)(d)
            f01(d)["expected"] = "fixtures/F02/expected.json"
        self.assertIn("F01: human review is stale (fixture_digest does not match the current items/expected)",
                      errs(changed_expected, verify=verify))
        changed = copy.deepcopy(EXPECTED)
        first = next(iter(changed))
        changed[first] = {"changed": True}
        self.assertNotEqual(rc.fixture_digest(f01(DOC)["items"], changed), digest)
        # main(): the CI entry point resolves signoffs, needs no network
        # without human reviews, and an unreachable GitHub is never a pass.
        import contextlib
        doc = copy.deepcopy(DOC)
        promote(good)(doc)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rc.main(["--verify-signoff"], doc=doc, fetch=fake_fetch()), 0)
            self.assertEqual(rc.main([], doc=doc), 1)

            def unreachable(path):
                raise RuntimeError("gh: not logged in")
            self.assertEqual(rc.main(["--verify-signoff"], doc=doc, fetch=unreachable), 1)
            self.assertEqual(rc.main(["--verify-signoff"], doc=copy.deepcopy(DOC), fetch=unreachable), 0)

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
