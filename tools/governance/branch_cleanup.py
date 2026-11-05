"""Safe post-merge source-branch cleanup.

A branch may be deleted only if ALL of these hold (docs/DEVELOPMENT.md):
  * the PR is merged (merged == true);
  * the PR head lives in this repository (not a fork);
  * the branch is not the default branch, not ``main`` and not protected;
  * the current remote branch head equals the merged PR's head SHA
    (no new commits; squash merges are NOT inferred from merge-base);
  * no other open PR uses the branch as head or base.

Default mode is a dry run. ``--execute`` re-reads all state, then deletes with
a server-side SHA lease: ``git push --force-with-lease=refs/heads/<b>:<sha>
<remote> :refs/heads/<b>``. The git receive-pack server compares the ref's
current value with the expected merged-head SHA under the ref lock and rejects
the delete if anything was pushed after the last read, so a racing push is
never lost. After the push the ref's absence is verified via ``git ls-remote``
and the REST API. Any failure (API/parse error, rejected lease, permission
error, ref still present, remote not pointing at --repo) is reported as
``cleanup_pending`` with exit status 2; nothing is reported as done unless the
ref is verified gone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

NEVER_DELETE = {"main", "master"}
SHA_RE = re.compile(r"[0-9a-f]{40}")
CMD_TIMEOUT_SEC = 120


@dataclass
class Decision:
    safe: bool
    branch: str
    reasons: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.safe:
            return "deletable"
        if self.reasons == ["branch already deleted"]:
            return "done"
        return "cleanup_pending"


def decide(
    repo: str,
    pr: dict,
    branch_info: dict | None,
    open_prs: list[dict],
    default_branch: str,
) -> Decision:
    """Pure decision function. ``branch_info`` is None when the ref is absent."""
    head = pr.get("head") or {}
    branch = head.get("ref", "")
    reasons: list[str] = []

    if not pr.get("merged"):
        reasons.append("PR is not merged")
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo != repo:
        reasons.append(f"head repo {head_repo!r} is not {repo!r} (fork or deleted repo)")
    if not branch:
        reasons.append("PR has no head ref")
    if not SHA_RE.fullmatch(str(head.get("sha") or "")):
        reasons.append(f"PR head SHA {head.get('sha')!r} is not a full commit SHA")
    if branch == default_branch or branch in NEVER_DELETE:
        reasons.append(f"branch {branch!r} is default/main")
    if branch_info is None:
        if not reasons:
            return Decision(False, branch, ["branch already deleted"])
        return Decision(False, branch, reasons)
    if branch_info.get("protected"):
        reasons.append("branch is protected")
    current = (branch_info.get("commit") or {}).get("sha")
    if current != head.get("sha"):
        reasons.append(
            f"branch head {current} != merged PR head {head.get('sha')} (new commits)"
        )
    for other in open_prs:
        if other.get("number") == pr.get("number"):
            continue
        o_head = other.get("head") or {}
        o_base = other.get("base") or {}
        if (o_head.get("ref") == branch and (o_head.get("repo") or {}).get("full_name") == repo) or o_base.get("ref") == branch:
            reasons.append(f"branch used by open PR #{other.get('number')}")
    return Decision(not reasons, branch, reasons)


Runner = Callable[..., subprocess.CompletedProcess]


class CleanupError(RuntimeError):
    """Any condition that must end as cleanup_pending."""


def parse_json_stream(text: str):
    """Parse gh output that may hold several concatenated JSON documents.

    ``gh api --paginate`` (gh < 2.48 has no --slurp) prints one JSON array per
    page back to back, e.g. ``[...]\n[...]``. Arrays are flattened into one
    list; a single non-array document is returned as-is.
    """
    dec = json.JSONDecoder()
    docs, i, n = [], 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            doc, i = dec.raw_decode(text, i)
        except json.JSONDecodeError as e:
            raise CleanupError(f"unparseable gh output at offset {i}: {e.msg}") from e
        docs.append(doc)
    if not docs:
        return {}
    if all(isinstance(d, list) for d in docs):
        return [x for d in docs for x in d]
    if len(docs) == 1:
        return docs[0]
    raise CleanupError("gh output held several non-array JSON documents")


def _run(runner: Runner, cmd: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        return runner(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT_SEC, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise CleanupError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} could not run: {e}") from e


def _gh(args: list[str], runner: Runner = subprocess.run, allow_404: bool = False):
    proc = _run(runner, ["gh", "api", *args])
    if proc.returncode != 0:
        if allow_404 and ("HTTP 404" in proc.stderr or "Not Found" in proc.stderr):
            return None
        raise CleanupError(f"gh api {' '.join(args)} failed: {proc.stderr.strip()}")
    return parse_json_stream(proc.stdout)


def gather(repo: str, number: int, runner: Runner = subprocess.run):
    pr = _gh([f"repos/{repo}/pulls/{number}"], runner)
    meta = _gh([f"repos/{repo}"], runner)
    branch = (pr.get("head") or {}).get("ref", "")
    info = _gh([f"repos/{repo}/branches/{quote(branch, safe='/')}"], runner, allow_404=True) if branch else None
    open_prs = _gh([f"repos/{repo}/pulls?state=open&per_page=100", "--paginate"], runner) or []
    if not isinstance(open_prs, list):
        raise CleanupError("open PR listing is not a JSON array")
    return pr, info, open_prs, meta["default_branch"]


def remote_matches_repo(url: str, repo: str) -> bool:
    m = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", url.strip())
    return bool(m) and m.group(1).lower() == repo.lower()


def lease_delete(repo: str, branch: str, expected_sha: str, remote: str, runner: Runner = subprocess.run) -> None:
    """Delete refs/heads/<branch> only if it still points at expected_sha."""
    # Pushes go to the push URL(s) (pushurl / pushInsteadOf), not the fetch URL.
    urls = _run(runner, ["git", "remote", "get-url", "--push", "--all", remote])
    push_urls = [u for u in urls.stdout.splitlines() if u.strip()] if urls.returncode == 0 else []
    if not push_urls or not all(remote_matches_repo(u, repo) for u in push_urls):
        raise CleanupError(f"git remote {remote!r} push URL(s) do not all point at {repo}")
    ref = f"refs/heads/{branch}"
    push = _run(runner, ["git", "push", "--porcelain", f"--force-with-lease={ref}:{expected_sha}", remote, f":{ref}"])
    if push.returncode != 0:
        detail = (push.stdout + push.stderr).strip().splitlines()
        raise CleanupError("leased delete rejected: " + (detail[-1] if detail else f"exit {push.returncode}"))
    ls = _run(runner, ["git", "ls-remote", "--heads", remote, ref])
    if ls.returncode != 0:
        raise CleanupError(f"cannot verify deletion: git ls-remote failed: {ls.stderr.strip()}")
    if ls.stdout.strip():
        raise CleanupError("branch still exists after delete (git ls-remote)")
    if _gh([f"repos/{repo}/branches/{quote(branch, safe='/')}"], runner, allow_404=True) is not None:
        raise CleanupError("branch still exists after delete (REST API)")


def _pending(reasons: list[str]) -> int:
    print(json.dumps({"state": "cleanup_pending", "reasons": reasons}))
    return 2


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--remote", default="origin", help="git remote of --repo used for the leased delete")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv)

    try:
        pr, info, open_prs, default = gather(args.repo, args.pr, runner)
    except (CleanupError, KeyError, TypeError, AttributeError) as e:
        return _pending([f"could not read state: {e}"])
    d = decide(args.repo, pr, info, open_prs, default)
    print(json.dumps({"pr": args.pr, "branch": d.branch, "state": d.state, "reasons": d.reasons}))
    if not d.safe or not args.execute:
        return 0 if d.state in ("deletable", "done") else 2

    try:
        pr2, info2, open2, default2 = gather(args.repo, args.pr, runner)
    except (CleanupError, KeyError, TypeError, AttributeError) as e:
        return _pending([f"could not re-read state: {e}"])
    d2 = decide(args.repo, pr2, info2, open2, default2)
    if not d2.safe:
        return _pending(["state changed before delete", *d2.reasons])
    expected = pr2["head"]["sha"]
    try:
        lease_delete(args.repo, d2.branch, expected, args.remote, runner)
    except CleanupError as e:
        return _pending([str(e)])
    print(json.dumps({"state": "done", "branch": d2.branch, "deleted_head": expected}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
