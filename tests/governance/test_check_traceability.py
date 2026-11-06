import copy
import unittest

import yaml

from tools.governance import check_traceability as ct

SPEC = "| R01 | x |\n| T01 U | y |\n| T100 U/Q | z |\n| A01 | w |\n"
IDS = ct.spec_ids(SPEC)


def entry(i, **kw):
    e = {"id": i, "title": "t", "owners": [4], "release_owner": 15, "status": "not_started",
         "evidence": [], "note": None}
    e.update(kw)
    return e


GOOD = {"requirements": [entry("R01")], "tests": [entry("T01"), entry("T100")], "acceptance": [entry("A01")]}


RUN_ID = 42146572035
RUN = f"https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/{RUN_ID}"
SHA = "ab" * 20


def fake_github(run=None, compare="ahead"):
    """Stand-in for gh api: only RUN_ID exists, as a successful main run at SHA."""
    repo = {"full_name": ct.REPO}

    def fetch(path):
        if path == f"repos/{ct.REPO}/actions/runs/{RUN_ID}":
            return {"repository": repo, "head_repository": repo, "head_branch": "main", "head_sha": SHA,
                    "status": "completed", "conclusion": "success", **(run or {})}
        if path == f"repos/{ct.REPO}/compare/{SHA}...main":
            return {"status": compare}
        raise RuntimeError(f"HTTP 404: {path}")

    return fetch


def errs(mutate, verify=None):
    doc = copy.deepcopy(GOOD)
    mutate(doc)
    return ct.check(doc, IDS, verify=verify)


class TraceabilityTests(unittest.TestCase):
    def test_spec_ids_parsed(self):
        self.assertEqual(IDS, {"R": {"R01"}, "T": {"T01", "T100"}, "A": {"A01"}})

    def test_good(self):
        self.assertEqual(ct.check(GOOD, IDS), [])

    def test_missing_extra_duplicate(self):
        self.assertTrue(any("missing SPEC ids ['T100']" in e for e in errs(lambda d: d["tests"].pop())))
        self.assertTrue(any("not in SPEC ['T02']" in e for e in errs(lambda d: d["tests"].append(entry("T02")))))
        self.assertTrue(any("duplicate" in e for e in errs(lambda d: d["tests"].append(entry("T01")))))

    def test_partial_needs_evidence_and_note(self):
        got = errs(lambda d: d["tests"][0].update(status="partial"))
        self.assertTrue(any("requires evidence" in e for e in got))
        self.assertTrue(any("requires a note" in e for e in got))

    def test_passed_needs_main_commit_run_and_tests(self):
        branch_only = [{"scope": "contract", "branch": "issue-4-contracts",
                        "tests": ["tests/governance/test_check_traceability.py"]}]
        got = errs(lambda d: d["tests"][0].update(status="passed", evidence=branch_only, note="n"))
        self.assertTrue(any("passed requires main evidence" in e for e in got))
        good = [{"branch": "main", "commit": SHA, "run": RUN,
                 "tests": ["tests/governance/test_check_traceability.py"]}]
        doc = copy.deepcopy(GOOD)
        doc["tests"][0].update(status="passed", evidence=good, note="n")
        # Offline, well-formed evidence is still not enough for passed.
        self.assertTrue(any("verified provenance" in e for e in ct.check(doc, IDS)))
        self.assertEqual(ct.check(doc, IDS, verify=ct.github_verifier(fake_github())), [])

    def test_pm_fake_passed_example_rejected(self):
        """PM #32 review: note='fake', branch main, made-up commit, run 0 and a
        named test in a non-test file must not pass."""
        ev = [{"branch": "main", "commit": "ab" * 20,
               "run": "https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/0",
               "tests": ["LICENSE::NoSuchTest"]}]
        doc = copy.deepcopy(GOOD)
        doc["tests"][0].update(status="passed", evidence=ev, note="fake")
        offline = ct.check(doc, IDS)
        self.assertTrue(any("named tests are only supported" in e for e in offline), offline)
        self.assertTrue(any("verified provenance" in e for e in offline), offline)
        online = ct.check(doc, IDS, verify=ct.github_verifier(fake_github()))
        self.assertTrue(any("could not verify" in e for e in online), online)

    def test_github_verifier_checks_run_and_commit(self):
        ev = {"branch": "main", "commit": SHA, "run": RUN, "tests": ["x"]}
        self.assertEqual(ct.github_verifier(fake_github())(ev), [])
        cases = {
            "failed run": ({"conclusion": "failure"}, "did not succeed"),
            "in progress": ({"status": "in_progress", "conclusion": None}, "did not succeed"),
            "branch run": ({"head_branch": "issue-4-x"}, "not main"),
            "other sha": ({"head_sha": "cd" * 20}, "head_sha does not match"),
            "fork run": ({"head_repository": {"full_name": "someone/fork"}}, "not from this repository"),
        }
        for label, (change, msg) in cases.items():
            with self.subTest(label):
                got = ct.github_verifier(fake_github(run=change))(ev)
                self.assertTrue(any(msg in e for e in got), got)
        got = ct.github_verifier(fake_github(compare="diverged"))(ev)
        self.assertTrue(any("not on main" in e for e in got), got)

    def test_go_named_tests(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=ct.ROOT) as d:
            f = Path(d) / "x_test.go"
            f.write_text("package x\n\nfunc TestReal(t *testing.T) {}\nfunc helper() {}\n", encoding="utf-8")
            rel = f.relative_to(ct.ROOT).as_posix()
            self.assertIsNone(ct._named_test_problem(rel, f"{rel}::TestReal"))
            self.assertIn("test not found", ct._named_test_problem(rel, f"{rel}::TestMissing"))
            self.assertIn("test not found", ct._named_test_problem(rel, f"{rel}::helper"))

    def test_fake_passed_evidence_rejected(self):
        fakes = {
            "all-zero commit": {"commit": "0" * 40},
            "foreign run": {"run": "https://github.com/"},
            "other repo run": {"run": "https://github.com/x/y/actions/runs/1"},
            "no tests": {"tests": []},
            "missing test file": {"tests": ["tests/nope/test_missing.py::T"]},
        }
        for label, change in fakes.items():
            ev = {"branch": "main", "commit": SHA, "run": RUN,
                  "tests": ["tests/governance/test_check_traceability.py"], **change}
            with self.subTest(label):
                # Verified provenance, so only the fake field can cause the rejection.
                self.assertTrue(errs(lambda d: d["tests"][0].update(status="passed", evidence=[ev], note="n"),
                                     verify=ct.github_verifier(fake_github())))

    def test_named_test_must_exist(self):
        def ev(t):
            return [{"scope": "contract", "branch": "main", "tests": [t]}]
        here = "tests/governance/test_check_traceability.py"
        for ok in (f"{here}::TraceabilityTests", f"{here}::TraceabilityTests.test_good", here):
            with self.subTest(ok=ok):
                self.assertEqual(errs(lambda d: d["tests"][0].update(status="partial", evidence=ev(ok), note="n")), [])
        for bad in (f"{here}::NoSuchClass", f"{here}::TraceabilityTests.test_renamed_away",
                    f"{here}::test_good"):
            with self.subTest(bad=bad):
                got = errs(lambda d: d["tests"][0].update(status="partial", evidence=ev(bad), note="n"))
                self.assertTrue(any("test not found" in e for e in got), got)

    def test_non_mapping_evidence_and_basic_fields(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(evidence=["junk"])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(title="")))
        self.assertTrue(errs(lambda d: d["tests"][0].update(release_owner=14)))

    def test_owners(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[15])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[True])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[])))
        self.assertEqual(errs(lambda d: d["tests"][0].update(owners=[], note="unassigned: ask PM")), [])

    def test_primary_must_be_an_owner(self):
        self.assertEqual(errs(lambda d: d["tests"][0].update(primary=4)), [])
        self.assertTrue(errs(lambda d: d["tests"][0].update(primary=9)))

    def test_bad_status(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(status="done")))

    def test_repo_file_is_valid(self):
        self.assertEqual(ct.main(), 0)


if __name__ == "__main__":
    unittest.main()
