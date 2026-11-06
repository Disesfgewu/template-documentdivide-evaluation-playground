import base64
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


HERE = "tests/governance/test_check_traceability.py"


def contents(text):
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def fake_github(run=None, compare="ahead", files=None):
    """Stand-in for gh api: only RUN_ID exists, as a successful main run at SHA.
    ``files`` maps path -> source at SHA (None: absent); by default SHA holds
    the current checkout's files."""
    repo = {"full_name": ct.REPO}

    def fetch(path):
        prefix, ref = f"repos/{ct.REPO}/contents/", f"?ref={SHA}"
        if path.startswith(prefix) and path.endswith(ref):
            rel = path[len(prefix):-len(ref)]
            if files is not None and rel in files:
                if files[rel] is None:
                    raise RuntimeError(f"HTTP 404: {path}")
                return contents(files[rel])
            if (ct.ROOT / rel).is_file():
                return contents((ct.ROOT / rel).read_text(encoding="utf-8"))
            raise RuntimeError(f"HTTP 404: {path}")
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

    def test_named_glob_rejected(self):
        """PM-REVIEW 6676131274: a glob with a named test skipped name
        validation, so a nonexistent test passed with a stub verifier."""
        ev = [{"branch": "main", "commit": "64da938ca7506964e7170ae3b51d046eb20c895b",
               "run": "https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/42151272974",
               "tests": ["tests/governance/test_*.py::NoSuchTest"]}]
        doc = copy.deepcopy(GOOD)
        doc["tests"][0].update(status="passed", evidence=ev, note="glob")
        got = ct.check(doc, IDS, verify=lambda _: [])
        self.assertIn("tests[T01].evidence[0]: named tests need a concrete file, not a glob: "
                      "tests/governance/test_*.py::NoSuchTest", got)
        # An unnamed glob stays allowed.
        ev[0]["tests"] = ["tests/governance/test_*.py"]
        self.assertEqual(ct.check(doc, IDS, verify=lambda _: []), [])

    def test_passed_ci_path(self):
        """The CI entrypoint runs --verify-provenance, so valid passed evidence
        passes there and fake evidence fails; offline mode still rejects it."""
        import contextlib, io
        from pathlib import Path
        script = (Path(ct.ROOT) / "scripts" / "check.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m tools.governance.check_traceability --verify-provenance", script)
        doc = copy.deepcopy(GOOD)
        doc["tests"][0].update(status="passed", note="covered by the named test", evidence=[
            {"branch": "main", "commit": SHA, "run": RUN,
             "tests": ["tests/governance/test_check_traceability.py::TraceabilityTests.test_passed_ci_path"]}])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ct.main(["--verify-provenance"], doc=doc, spec_text=SPEC, fetch=fake_github()), 0)
            self.assertEqual(ct.main([], doc=doc, spec_text=SPEC), 1)
            bad = fake_github(run={"conclusion": "failure"})
            self.assertEqual(ct.main(["--verify-provenance"], doc=doc, spec_text=SPEC, fetch=bad), 1)

            def unreachable(path):
                raise RuntimeError("gh: not logged in")
            self.assertEqual(ct.main(["--verify-provenance"], doc=doc, spec_text=SPEC, fetch=unreachable), 1)
            # No passed entries: provenance mode never calls GitHub.
            self.assertEqual(ct.main(["--verify-provenance"], doc=copy.deepcopy(GOOD), spec_text=SPEC,
                                     fetch=unreachable), 0)

    def test_github_verifier_checks_run_and_commit(self):
        ev = {"branch": "main", "commit": SHA, "run": RUN, "tests": [HERE]}
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

    def test_passed_tests_must_exist_at_evidence_commit(self):
        """PM-REVIEW on #32 (673ceca): T01 cited run 42151272974 / 64da938 and a
        test added after that commit; the checker looked at the PR checkout,
        so the stale reference was accepted. Tests are now read at the SHA."""
        named = f"{HERE}::TraceabilityTests.test_named_glob_rejected"

        def verify(files, tests):
            ev = {"branch": "main", "commit": SHA, "run": RUN, "tests": tests}
            return ct.github_verifier(fake_github(files=files))(ev)

        # Test file introduced after the run's commit: absent at SHA.
        got = verify({HERE: None}, [named])
        self.assertTrue(any(f"{HERE} does not exist at {SHA[:7]}" in e for e in got), got)
        # File existed, but the named test was added later.
        older = "import unittest\n\nclass TraceabilityTests(unittest.TestCase):\n    def test_good(self):\n        pass\n"
        got = verify({HERE: older}, [named])
        self.assertIn(f"provenance: at {SHA[:7]}: test not found: {named}", got)
        # Positive historical reference: the name existed at SHA, even though
        # that file's content differs from the current checkout.
        self.assertEqual(verify({HERE: older}, [f"{HERE}::TraceabilityTests.test_good"]), [])
        self.assertEqual(verify({HERE: older}, [HERE]), [])
        # Globs cannot be resolved at a commit, so passed evidence rejects them.
        got = verify(None, ["tests/governance/test_*.py"])
        self.assertTrue(any("not a glob" in e for e in got), got)
        # Through check(): the PM's T01 shape is rejected, and main() labels a
        # verified entry as provenance, not proof of execution.
        doc = copy.deepcopy(GOOD)
        doc["tests"][0].update(status="passed", note="T01", evidence=[
            {"branch": "main", "commit": SHA, "run": RUN, "tests": [named]}])
        got = ct.check(doc, IDS, verify=ct.github_verifier(fake_github(files={HERE: None})))
        self.assertTrue(any(e.startswith("tests[T01]: provenance:") for e in got), got)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ct.main(["--verify-provenance"], doc=doc, spec_text=SPEC, fetch=fake_github()), 0)
        self.assertIn("T01 passed: run provenance and tests-at-commit verified; "
                      "execution of the cited tests is not log-verified", out.getvalue())

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
