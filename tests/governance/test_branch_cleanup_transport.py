"""Transport/CLI-level tests for tools.governance.branch_cleanup.

A fake runner stands in for `gh api` and `git` so the full main() path is
exercised: pagination parsing, the SHA-leased delete and post-delete checks.
The fake git server applies the same rule as git receive-pack: a leased push
is accepted only if the ref's current value equals the expected old SHA.
These are simulations, not calls against the real GitHub API.
"""
import contextlib
import io
import json
import subprocess
import unittest

from tools.governance import branch_cleanup as bc

REPO = "o/r"
BRANCH = "issue-7-x"
HEAD = "a" * 40
NEW = "b" * 40


def _cp(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


class FakeGitHub:
    def __init__(self, open_pr_pages=None):
        self.branches = {BRANCH: HEAD, "main": "c" * 40}
        self.push_urls = ["git@github.com:o/r.git"]
        self.missing_binary = False
        self.pr = {
            "number": 7,
            "merged": True,
            "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        }
        self.open_pr_pages = open_pr_pages if open_pr_pages is not None else [[]]
        self.raw_open_prs = None
        self.calls = []
        self.before_push = None
        self.push_failure = None
        self.ignore_delete = False
        self.ls_remote_fails = False

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:2] == ["gh", "api"]:
            return self._gh(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            self.get_url_args = cmd
            if self.missing_binary and cmd[0] == "git":
                raise FileNotFoundError("git")
            return _cp(cmd, out="".join(u + "\n" for u in self.push_urls))
        if cmd[:2] == ["git", "push"]:
            return self._push(cmd)
        if cmd[:2] == ["git", "ls-remote"]:
            if self.ls_remote_fails:
                return _cp(cmd, 128, err="fatal: could not read from remote")
            ref = cmd[-1].removeprefix("refs/heads/")
            sha = self.branches.get(ref)
            return _cp(cmd, out=f"{sha}\trefs/heads/{ref}\n" if sha else "")
        raise AssertionError(f"unexpected command {cmd}")

    def _gh(self, cmd):
        path = cmd[2]
        if "-X" in cmd:
            raise AssertionError("REST ref deletion must not be used")
        if path == f"repos/{REPO}/pulls/7":
            return _cp(cmd, out=json.dumps(self.pr))
        if path == f"repos/{REPO}":
            return _cp(cmd, out=json.dumps({"default_branch": "main"}))
        if path.startswith(f"repos/{REPO}/branches/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.branches:
                return _cp(cmd, 1, err="gh: Not Found (HTTP 404)")
            return _cp(cmd, out=json.dumps({"commit": {"sha": self.branches[name]}, "protected": False}))
        if path.startswith(f"repos/{REPO}/pulls?state=open"):
            if self.raw_open_prs is not None:
                return _cp(cmd, out=self.raw_open_prs)
            # gh --paginate prints one JSON array per page, back to back.
            return _cp(cmd, out="".join(json.dumps(p) + "\n" for p in self.open_pr_pages))
        raise AssertionError(f"unexpected gh path {path}")

    def _push(self, cmd):
        if self.before_push:
            self.before_push(self)
        if self.push_failure:
            return _cp(cmd, 1, err=self.push_failure)
        lease = next(a for a in cmd if a.startswith("--force-with-lease="))
        ref, expected = lease.removeprefix("--force-with-lease=").split(":")
        name = ref.removeprefix("refs/heads/")
        if self.branches.get(name) != expected:
            return _cp(cmd, 1, out=f"!\t:{ref}\t[rejected] (stale info)\n")
        if not self.ignore_delete:
            del self.branches[name]
        return _cp(cmd, out=f"-\t:{ref}\t[deleted]\nDone\n")

    def pushes(self):
        return [c for c in self.calls if c[:2] == ["git", "push"]]


def run_main(fake, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bc.main(["--repo", REPO, "--pr", "7", *extra], runner=fake)
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return rc, lines


class LeasedDeleteTests(unittest.TestCase):
    def test_unchanged_head_is_deleted_with_sha_lease_and_verified(self):
        fake = FakeGitHub()
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out[-1]["state"], "done")
        self.assertNotIn(BRANCH, fake.branches)
        self.assertEqual(len(fake.pushes()), 1)
        self.assertIn(f"--force-with-lease=refs/heads/{BRANCH}:{HEAD}", fake.pushes()[0])

    def test_push_after_last_read_is_rejected_and_not_reported_done(self):
        fake = FakeGitHub()
        fake.before_push = lambda f: f.branches.__setitem__(BRANCH, NEW)
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["state"], "cleanup_pending")
        self.assertIn("rejected", out[-1]["reasons"][0])
        self.assertEqual(fake.branches[BRANCH], NEW, "the racing commit must survive")

    def test_permission_failure_is_pending(self):
        fake = FakeGitHub()
        fake.push_failure = "remote: Permission to o/r.git denied"
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["state"], "cleanup_pending")
        self.assertIn(BRANCH, fake.branches)

    def test_ref_still_present_after_accepted_push_is_pending(self):
        fake = FakeGitHub()
        fake.ignore_delete = True
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertIn("still exists", out[-1]["reasons"][0])

    def test_unverifiable_deletion_is_pending(self):
        fake = FakeGitHub()
        fake.ls_remote_fails = True
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertIn("cannot verify", out[-1]["reasons"][0])

    def test_remote_pointing_elsewhere_never_pushes(self):
        fake = FakeGitHub()
        fake.push_urls = ["git@github.com:someone/fork.git"]
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(fake.pushes(), [])
        self.assertIn(BRANCH, fake.branches)

    def test_any_push_url_elsewhere_never_pushes(self):
        fake = FakeGitHub()
        fake.push_urls = ["git@github.com:o/r.git", "git@github.com:someone/mirror.git"]
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(fake.pushes(), [])
        self.assertIn("--push", fake.get_url_args)

    def test_missing_binary_is_pending_not_crash(self):
        fake = FakeGitHub()
        fake.missing_binary = True
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["state"], "cleanup_pending")

    def test_missing_head_sha_is_never_deletable(self):
        fake = FakeGitHub()
        del fake.pr["head"]["sha"]
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(fake.pushes(), [])

    def test_dry_run_never_pushes(self):
        fake = FakeGitHub()
        rc, out = run_main(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(out[-1]["state"], "deletable")
        self.assertEqual(fake.pushes(), [])


class PaginationTests(unittest.TestCase):
    def _filler(self, n):
        return [
            {"number": 1000 + i, "head": {"ref": f"other-{i}", "repo": {"full_name": REPO}}, "base": {"ref": "main"}}
            for i in range(n)
        ]

    def test_blocking_pr_on_second_page_prevents_delete(self):
        blocker = {"number": 99, "head": {"ref": "stacked", "repo": {"full_name": REPO}}, "base": {"ref": BRANCH}}
        fake = FakeGitHub(open_pr_pages=[self._filler(100), [blocker]])
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertTrue(any("#99" in r for r in out[-1]["reasons"]), out)
        self.assertEqual(fake.pushes(), [])

    def test_unparseable_listing_is_pending_not_crash(self):
        fake = FakeGitHub()
        fake.raw_open_prs = '[{"number": 1}]\n[{"number": '
        rc, out = run_main(fake, "--execute")
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["state"], "cleanup_pending")
        self.assertEqual(fake.pushes(), [])


class ParseJsonStreamTests(unittest.TestCase):
    def test_concatenated_pages_are_flattened(self):
        self.assertEqual(bc.parse_json_stream('[{"n":1}]\n[{"n":2}]\n'), [{"n": 1}, {"n": 2}])

    def test_single_object(self):
        self.assertEqual(bc.parse_json_stream('{"a": 1}\n'), {"a": 1})

    def test_empty_output(self):
        self.assertEqual(bc.parse_json_stream("  \n"), {})

    def test_several_objects_rejected(self):
        with self.assertRaises(bc.CleanupError):
            bc.parse_json_stream('{"a":1}{"b":2}')

    def test_remote_url_matching(self):
        self.assertTrue(bc.remote_matches_repo("git@github.com:o/r.git", "o/r"))
        self.assertTrue(bc.remote_matches_repo("https://github.com/O/R", "o/r"))
        self.assertFalse(bc.remote_matches_repo("https://github.com/o/r-fork.git", "o/r"))
        self.assertFalse(bc.remote_matches_repo("https://evil.example/github.com/o/r", "o/r"))
        self.assertTrue(bc.remote_matches_repo("ssh://git@github.com/o/r.git", "o/r"))




class RealGitLeaseTests(unittest.TestCase):
    """lease_delete against a real git server process (local bare repo).

    Only `git remote get-url` (must name the GitHub repo) and the REST
    existence check are intercepted; `git push --force-with-lease` and
    `git ls-remote` run for real, so the expected-SHA check is enforced by
    git itself rather than by the fake above.
    """

    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.bare = os.path.join(root, "server.git")
        self.work = os.path.join(root, "work")
        self.other = os.path.join(root, "other")
        self._git(root, "init", "-q", "--bare", self.bare)
        self._git(root, "clone", "-q", self.bare, self.work)
        self._git(self.work, "commit", "-q", "--allow-empty", "-m", "base")
        self._git(self.work, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
        self.merged_head = self._git(self.work, "rev-parse", "HEAD").strip()
        self._git(root, "clone", "-q", "--branch", BRANCH, self.bare, self.other)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        p = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(f"git {args} failed: {p.stderr}")
        return p.stdout

    def _runner(self, cmd, **kw):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _cp(cmd, out="https://github.com/o/r.git\n")
        if cmd[:2] == ["gh", "api"]:
            exists = self._remote_head() is not None
            return _cp(cmd, out='{"commit": {}}') if exists else _cp(cmd, 1, err="HTTP 404")
        return subprocess.run(cmd, cwd=self.work, env=self.env, capture_output=True, text=True)

    def _remote_head(self):
        out = self._git(self.work, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}")
        return out.split()[0] if out.strip() else None

    def test_real_git_rejects_delete_after_racing_push(self):
        self._git(self.other, "commit", "-q", "--allow-empty", "-m", "late commit")
        self._git(self.other, "push", "-q", "origin", BRANCH)
        racing = self._git(self.other, "rev-parse", "HEAD").strip()
        # Refresh the work clone's tracking ref: a bare --force-with-lease (no
        # explicit SHA) would now accept the delete, so only an explicit
        # expected-SHA lease keeps this test passing.
        self._git(self.work, "fetch", "-q", "origin")
        with self.assertRaises(bc.CleanupError) as ctx:
            bc.lease_delete(REPO, BRANCH, self.merged_head, "origin", runner=self._runner)
        self.assertIn("rejected", str(ctx.exception))
        self.assertEqual(self._remote_head(), racing, "racing commit must survive")

    def test_real_git_deletes_when_head_matches(self):
        bc.lease_delete(REPO, BRANCH, self.merged_head, "origin", runner=self._runner)
        self.assertIsNone(self._remote_head())


if __name__ == "__main__":
    unittest.main()
