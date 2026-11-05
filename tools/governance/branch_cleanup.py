"""Safe post-merge source-branch cleanup.

A branch may be deleted only if ALL of these hold (docs/DEVELOPMENT.md):
  * the PR is merged (merged == true);
  * the PR head lives in this repository (not a fork);
  * the branch is not the default branch, not ``main`` and not protected;
  * the current remote branch head equals the merged PR's head SHA
    (no new commits; squash merges are NOT inferred from merge-base);
  * no other open PR uses the branch as head or base.

Default mode is a dry run. ``--execute`` re-reads the ref immediately before
deleting (compare-and-delete), then verifies the ref is gone. If any check
fails the branch is reported as ``cleanup_pending`` and nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field

NEVER_DELETE = {"main", "master"}


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


def _gh(args: list[str], allow_404: bool = False):
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        if allow_404 and ("HTTP 404" in proc.stderr or "Not Found" in proc.stderr):
            return None
        raise RuntimeError(f"gh api {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def gather(repo: str, number: int):
    pr = _gh([f"repos/{repo}/pulls/{number}"])
    meta = _gh([f"repos/{repo}"])
    branch = (pr.get("head") or {}).get("ref", "")
    info = _gh([f"repos/{repo}/branches/{branch}"], allow_404=True) if branch else None
    open_prs = _gh([f"repos/{repo}/pulls?state=open&per_page=100", "--paginate"]) or []
    return pr, info, open_prs, meta["default_branch"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv)

    pr, info, open_prs, default = gather(args.repo, args.pr)
    d = decide(args.repo, pr, info, open_prs, default)
    print(json.dumps({"pr": args.pr, "branch": d.branch, "state": d.state, "reasons": d.reasons}))
    if not d.safe or not args.execute:
        return 0 if d.state in ("deletable", "done") else 2

    # Compare-and-delete: re-read immediately before deleting.
    pr2, info2, open2, default2 = gather(args.repo, args.pr)
    d2 = decide(args.repo, pr2, info2, open2, default2)
    if not d2.safe:
        print(json.dumps({"state": "cleanup_pending", "reasons": ["state changed before delete", *d2.reasons]}))
        return 2
    _gh(["-X", "DELETE", f"repos/{args.repo}/git/refs/heads/{d2.branch}"])
    if _gh([f"repos/{args.repo}/branches/{d2.branch}"], allow_404=True) is not None:
        print(json.dumps({"state": "cleanup_pending", "reasons": ["branch still exists after delete"]}))
        return 2
    print(json.dumps({"state": "done", "branch": d2.branch, "deleted_head": pr2["head"]["sha"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
