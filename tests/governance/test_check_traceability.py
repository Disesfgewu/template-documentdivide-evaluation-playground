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


def errs(mutate):
    doc = copy.deepcopy(GOOD)
    mutate(doc)
    return ct.check(doc, IDS)


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
        run = "https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/1"
        good = [{"branch": "main", "commit": "ab" * 20, "run": run,
                 "tests": ["tests/governance/test_check_traceability.py"]}]
        self.assertEqual(errs(lambda d: d["tests"][0].update(status="passed", evidence=good, note="n")), [])

    def test_fake_passed_evidence_rejected(self):
        fakes = {
            "all-zero commit": {"commit": "0" * 40},
            "foreign run": {"run": "https://github.com/"},
            "other repo run": {"run": "https://github.com/x/y/actions/runs/1"},
            "no tests": {"tests": []},
            "missing test file": {"tests": ["tests/nope/test_missing.py::T"]},
        }
        run = "https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/1"
        for label, change in fakes.items():
            ev = {"branch": "main", "commit": "ab" * 20, "run": run,
                  "tests": ["tests/governance/test_check_traceability.py"], **change}
            with self.subTest(label):
                self.assertTrue(errs(lambda d: d["tests"][0].update(status="passed", evidence=[ev], note="n")))

    def test_non_mapping_evidence_and_basic_fields(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(evidence=["junk"])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(title="")))
        self.assertTrue(errs(lambda d: d["tests"][0].update(release_owner=14)))

    def test_owners(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[15])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[True])))
        self.assertTrue(errs(lambda d: d["tests"][0].update(owners=[])))
        self.assertEqual(errs(lambda d: d["tests"][0].update(owners=[], note="unassigned: ask PM")), [])

    def test_bad_status(self):
        self.assertTrue(errs(lambda d: d["tests"][0].update(status="done")))

    def test_repo_file_is_valid(self):
        self.assertEqual(ct.main(), 0)


if __name__ == "__main__":
    unittest.main()
