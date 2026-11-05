import unittest

from tools.governance.branch_cleanup import decide

REPO = "o/r"
SHA = "a" * 40


def pr(**kw):
    base = {
        "number": 7,
        "merged": True,
        "head": {"ref": "issue-7-x", "sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
    }
    base.update(kw)
    return base


def branch(sha=SHA, protected=False):
    return {"commit": {"sha": sha}, "protected": protected}


class DecideTests(unittest.TestCase):
    def test_safe_when_all_conditions_hold(self):
        d = decide(REPO, pr(), branch(), [], "main")
        self.assertTrue(d.safe, d.reasons)
        self.assertEqual(d.state, "deletable")

    def test_not_merged(self):
        d = decide(REPO, pr(merged=False), branch(), [], "main")
        self.assertFalse(d.safe)
        self.assertEqual(d.state, "cleanup_pending")

    def test_new_commit_after_merge_blocks(self):
        d = decide(REPO, pr(), branch(sha="b" * 40), [], "main")
        self.assertFalse(d.safe)
        self.assertTrue(any("new commits" in r for r in d.reasons))

    def test_fork_head_blocks(self):
        p = pr(head={"ref": "issue-7-x", "sha": SHA, "repo": {"full_name": "fork/r"}})
        self.assertFalse(decide(REPO, p, branch(), [], "main").safe)

    def test_deleted_fork_repo_blocks(self):
        p = pr(head={"ref": "issue-7-x", "sha": SHA, "repo": None})
        self.assertFalse(decide(REPO, p, branch(), [], "main").safe)

    def test_default_and_main_never_deleted(self):
        p = pr(head={"ref": "main", "sha": SHA, "repo": {"full_name": REPO}})
        self.assertFalse(decide(REPO, p, branch(), [], "main").safe)
        p = pr(head={"ref": "develop", "sha": SHA, "repo": {"full_name": REPO}})
        self.assertFalse(decide(REPO, p, branch(), [], "develop").safe)

    def test_protected_blocks(self):
        self.assertFalse(decide(REPO, pr(), branch(protected=True), [], "main").safe)

    def test_other_open_pr_using_branch_as_head_or_base_blocks(self):
        as_base = {"number": 9, "head": {"ref": "y", "repo": {"full_name": REPO}}, "base": {"ref": "issue-7-x"}}
        self.assertFalse(decide(REPO, pr(), branch(), [as_base], "main").safe)
        as_head = {"number": 10, "head": {"ref": "issue-7-x", "repo": {"full_name": REPO}}, "base": {"ref": "main"}}
        self.assertFalse(decide(REPO, pr(), branch(), [as_head], "main").safe)

    def test_same_named_fork_branch_in_other_pr_does_not_block(self):
        fork = {"number": 11, "head": {"ref": "issue-7-x", "repo": {"full_name": "fork/r"}}, "base": {"ref": "main"}}
        self.assertTrue(decide(REPO, pr(), branch(), [fork], "main").safe)

    def test_already_deleted_is_done(self):
        d = decide(REPO, pr(), None, [], "main")
        self.assertFalse(d.safe)
        self.assertEqual(d.state, "done")

    def test_absent_ref_on_unmerged_pr_is_pending_not_done(self):
        d = decide(REPO, pr(merged=False), None, [], "main")
        self.assertEqual(d.state, "cleanup_pending")


if __name__ == "__main__":
    unittest.main()
