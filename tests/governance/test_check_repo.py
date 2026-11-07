import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.governance import check_repo

ROOT = Path(__file__).resolve().parents[2]


class Fixture(unittest.TestCase):
    """Copies the real repository governance files into a temp dir and mutates them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for rel in check_repo.REQUIRED_FILES + [
            ".github/workflows/ci.yml",
            "docs/PDF_FIXTURE_ACCEPTANCE.md",
            "tools/governance/verified_identities.txt",
        ]:
            dst = self.tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, dst)
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)


class RealRepoTest(unittest.TestCase):
    def test_repository_passes(self):
        self.assertEqual(check_repo.run_all(ROOT), [])


class NegativeTests(Fixture):
    def test_baseline_copy_passes(self):
        self.assertEqual(check_repo.run_all(self.tmp), [])

    def test_missing_required_file(self):
        (self.tmp / ".github/CODEOWNERS").unlink()
        self.assertIn("missing required file: .github/CODEOWNERS", check_repo.run_all(self.tmp))

    def test_pr_template_missing_heading(self):
        p = self.tmp / ".github/pull_request_template.md"
        p.write_text(p.read_text(encoding="utf-8").replace("## 驗證", "## X"), encoding="utf-8")
        self.assertTrue(any("## 驗證" in e for e in check_repo.check_pr_template(self.tmp)))

    def test_empty_green_job_rejected(self):
        self.write(".github/workflows/ci.yml", "on: [pull_request]\njobs:\n  ok:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n")
        self.assertTrue(any("empty green" in e for e in check_repo.check_workflows(self.tmp)))

    def test_continue_on_error_and_or_true_rejected(self):
        self.write(".github/workflows/ci.yml", "on: [pull_request]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: pytest || true\n")
        errs = check_repo.check_workflows(self.tmp)
        self.assertTrue(any("continue-on-error" in e for e in errs))
        self.assertTrue(any("|| true" in e for e in errs))

    def test_no_workflow_rejected(self):
        (self.tmp / ".github/workflows/ci.yml").unlink()
        self.assertIn("no CI workflow found in .github/workflows", check_repo.check_workflows(self.tmp))

    def test_unverified_codeowner_rejected(self):
        self.write(".github/CODEOWNERS", "* @someone-unverified\n")
        self.assertTrue(any("not in verified_identities" in e for e in check_repo.check_codeowners(self.tmp)))

    def test_issue_template_duplicate_id_and_missing_label(self):
        self.write(".github/ISSUE_TEMPLATE/task.yml", "name: t\ndescription: d\nbody:\n  - type: input\n    id: a\n    attributes: {label: A}\n  - type: input\n    id: a\n    attributes: {}\n")
        errs = check_repo.check_issue_templates(self.tmp)
        self.assertTrue(any("duplicate id" in e for e in errs))
        self.assertTrue(any("missing attributes.label" in e for e in errs))

    def test_invalid_yaml_reported(self):
        self.write(".github/ISSUE_TEMPLATE/bug.yml", "name: [unclosed\n")
        self.assertTrue(any("invalid YAML" in e for e in check_repo.check_issue_templates(self.tmp)))

    def test_broken_markdown_link(self):
        self.write("docs/x.md", "see [spec](missing.md) and [ok](https://example.com) [a](#x)\n")
        errs = check_repo.run_all(self.tmp)
        self.assertEqual([e for e in errs if "broken" in e], ["docs/x.md: broken relative link 'missing.md'"])

    def test_secret_detected(self):
        token = "ghp_" + "A1b2" * 9
        self.write("leak.txt", f"token={token}\n")
        self.assertTrue(any("possible github token" in e for e in check_repo.run_all(self.tmp)))

    def test_untracked_files_are_scanned(self):
        (self.tmp / "new.md").write_text("[x](gone.md)\n", encoding="utf-8")
        self.assertIn("new.md: broken relative link 'gone.md'", check_repo.run_all(self.tmp))

    def test_gitignored_secret_not_scanned(self):
        (self.tmp / ".gitignore").write_text("local.env\n", encoding="utf-8")
        (self.tmp / "local.env").write_text("k=" + "AKIA" + "A" * 16, encoding="utf-8")
        self.assertEqual(check_repo.run_all(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
