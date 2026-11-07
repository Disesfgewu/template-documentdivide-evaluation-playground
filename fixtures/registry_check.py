"""Validate fixtures/registry.yaml and reproduce every registered item.

Generator items are rebuilt in memory and must match the pinned sha256 byte
for byte; file items must exist and match. Only modules under
fixtures.generators may be used as generators (no arbitrary imports).

A human review only promotes a fixture after its signoff record is resolved
(``--verify-signoff`` asks GitHub; offline, ``human: true`` is rejected). The
record must be a comment/review in this repository, on the Issue/PR the URL
names, written by ``by`` (a GitHub user, not a bot) who is an owner, member
or collaborator with write access according to GitHub, containing the line
``FIXTURE-SIGNOFF <fixture id> <fixture_digest>``. The digest covers the
generated items and the expected/golden content, so changing either after
signoff makes the review stale.
"""
import datetime
import hashlib
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "fixtures" / "registry.yaml"
REQUIRED_IDS = [f"F{i:02d}" for i in range(1, 13)]
STATUSES = {"planned", "partial", "available"}
ORIGINS = {"human", "script", "spec_table"}  # never "model"
REPO = "Disesfgewu/template-documentdivide-evaluation-playground"
# A human signoff must point at a GitHub comment/review in this repository
# whose author can be checked; a bare flag or free-text name is not traceable.
SIGNOFF_URL = re.compile(
    rf"https://github\.com/{re.escape(REPO)}/(issues|pull)/(\d+)"
    r"#(issuecomment|pullrequestreview|discussion_r)-?(\d+)")
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
# A signer must be trusted by the repository itself (GitHub metadata), so a
# public comment by anyone cannot promote a fixture.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
TRUSTED_PERMISSIONS = {"admin", "maintain", "write"}
# Identities that are automation, never a human reviewer.
NON_HUMAN_BY = re.compile(r"(agent|bot|model|claude|gemini|gpt|llm)", re.IGNORECASE)


def _inside_fixtures(rel) -> Path:
    if not isinstance(rel, str) or not rel:
        raise ValueError("path must be a non-empty string")
    path = (ROOT / rel).resolve()
    if not path.is_relative_to((ROOT / "fixtures").resolve()):
        raise ValueError(f"{rel!r} is outside fixtures/")
    return path


def item_bytes(item: dict) -> bytes:
    if "generator" in item:
        name, args = item["generator"], item.get("args", [])
        if (not isinstance(name, str) or name.endswith(".__init__")
                or not re.fullmatch(r"fixtures\.generators\.[a-z_][a-z0-9_]*", name)):
            raise ValueError(f"generator {name!r} is outside fixtures.generators")
        if not isinstance(args, list):
            raise ValueError("args must be a list")
        return importlib.import_module(name).build(*args)
    return _inside_fixtures(item["file"]).read_bytes()


def _expected_items(rel) -> dict:
    data = json.loads(_inside_fixtures(rel).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        raise ValueError("expected file needs an 'items' mapping")
    return items


def fixture_digest(items, expected) -> str:
    """sha256 over the sorted "item_id:sha256" lines of one fixture version
    plus the canonical JSON of its expected/golden items. A human review
    records this digest; changing any item or expected value makes it stale."""
    lines = sorted(f"{it.get('id')}:{it.get('sha256')}" for it in items if isinstance(it, dict))
    golden = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    lines.append("expected:" + hashlib.sha256(golden.encode("utf-8")).hexdigest())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def signoff_line(fid: str, digest: str) -> str:
    return f"FIXTURE-SIGNOFF {fid} {digest}"


def github_signoff_verifier(fetch=None):
    """``verify(fid, review, digest) -> problems`` resolving the signoff URL.
    ``fetch(path) -> dict`` defaults to ``gh api``; any failure is a problem
    (an unreachable record is not a signoff)."""
    if fetch is None:
        def fetch(path):
            out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or f"gh api {path} failed")
            return json.loads(out.stdout)

    def verify(fid, review, digest) -> list[str]:
        m = SIGNOFF_URL.fullmatch(str(review.get("signoff", "")))
        if not m:
            return [f"{fid}: signoff: not a signoff URL of this repository"]
        kind, number, anchor, rid = m.groups()
        if anchor == "issuecomment":
            path, parent_key, parent = f"repos/{REPO}/issues/comments/{rid}", "issue_url", f"/issues/{number}"
        elif anchor == "pullrequestreview" and kind == "pull":
            path, parent_key, parent = (f"repos/{REPO}/pulls/{number}/reviews/{rid}",
                                        "pull_request_url", f"/pulls/{number}")
        elif anchor == "discussion_r" and kind == "pull":
            path, parent_key, parent = f"repos/{REPO}/pulls/comments/{rid}", "pull_request_url", f"/pulls/{number}"
        else:
            return [f"{fid}: signoff: {anchor} anchors need a pull URL"]
        try:
            record = fetch(path)
        except Exception as exc:
            return [f"{fid}: signoff record could not be resolved ({exc})"]
        problems = []
        if not str(record.get(parent_key, "")).endswith(f"repos/{REPO}{parent}"):
            problems.append(f"{fid}: signoff record is not on {kind}/{number}")
        user = record.get("user") or {}
        if user.get("login") != review.get("by") or user.get("type") != "User":
            problems.append(f"{fid}: signoff author {user.get('login')!r} ({user.get('type')}) "
                            f"is not reviewer {review.get('by')!r}")
        # The registry's ``by`` is only a claim: the signer must also be
        # trusted by this repository, judged from GitHub, not the registry.
        login = user.get("login")
        if record.get("author_association") not in TRUSTED_ASSOCIATIONS:
            problems.append(f"{fid}: signoff author {login!r} is not a repository owner/member/collaborator "
                            f"(author_association={record.get('author_association')!r})")
        else:
            try:
                perm = fetch(f"repos/{REPO}/collaborators/{login}/permission")
            except Exception as exc:
                problems.append(f"{fid}: signoff author permission could not be resolved ({exc})")
            else:
                if perm.get("permission") not in TRUSTED_PERMISSIONS:
                    problems.append(f"{fid}: signoff author {login!r} lacks write access "
                                    f"(permission={perm.get('permission')!r})")
        lines = [ln.strip() for ln in str(record.get("body") or "").splitlines()]
        if signoff_line(fid, digest) not in lines:
            problems.append(f"{fid}: signoff record does not contain '{signoff_line(fid, digest)}'")
        return problems

    return verify


def _review_errors(fid, review, items, expected, today: datetime.date, verify=None) -> list[str]:
    errors = []
    if not isinstance(review, dict):
        return [f"{fid}: golden.review must be a mapping"]
    human, by = review.get("human"), review.get("by")
    if not isinstance(human, bool) or not isinstance(by, str) or not by.strip():
        errors.append(f"{fid}: golden.review needs human (bool) and by")
    raw = review.get("date")
    try:
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise ValueError
        when = datetime.date.fromisoformat(raw)
    except ValueError:
        errors.append(f"{fid}: golden.review.date must be a real calendar date (YYYY-MM-DD), got {raw!r}")
    else:
        if when > today:
            errors.append(f"{fid}: golden.review.date {raw} is in the future")
    method = review.get("method")
    if not isinstance(method, str) or not method.strip():
        errors.append(f"{fid}: golden.review.method must describe how the golden was checked")
    if human is True:
        if (not isinstance(by, str) or not GITHUB_LOGIN.fullmatch(by)
                or NON_HUMAN_BY.search(by)):
            errors.append(f"{fid}: human review 'by' must be the reviewer's GitHub login, not an automation identity")
        if not isinstance(review.get("signoff"), str) or not SIGNOFF_URL.fullmatch(review["signoff"]):
            errors.append(f"{fid}: human review needs signoff = URL of the reviewer's GitHub comment/review in {REPO}")
        digest = review.get("fixture_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"{fid}: human review needs fixture_digest (sha256 of the reviewed items)")
        elif digest != fixture_digest(items, expected):
            errors.append(f"{fid}: human review is stale (fixture_digest does not match the current items/expected)")
        if not errors:
            # Well-formed is not signed off: the record itself must be read.
            if verify is None:
                errors.append(f"{fid}: human review requires a resolved signoff (run with --verify-signoff)")
            else:
                errors.extend(verify(fid, review, digest))
    return errors


def check(doc: dict, reproduce: bool = True, today: datetime.date | None = None,
          verify_signoff=None) -> list[str]:
    """``verify_signoff`` (see github_signoff_verifier) resolves human
    signoffs; without it a ``human: true`` review is never accepted."""
    today = today or datetime.date.today()
    errors = []
    entries = doc.get("fixtures") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return ["registry: missing fixtures list"]
    ids = [e.get("id") if isinstance(e, dict) else None for e in entries]
    if ids != REQUIRED_IDS:
        errors.append(f"registry: fixtures must be exactly {REQUIRED_IDS} in order, got {ids}")
    seen_items = set()
    for e in entries:
        if not isinstance(e, dict):
            errors.append("registry: every fixture entry must be a mapping")
            continue
        fid, status = e.get("id"), e.get("status")
        if status not in STATUSES:
            errors.append(f"{fid}: bad status {status!r}")
            continue
        if not e.get("missing") and status != "available":
            errors.append(f"{fid}: {status} must list what is missing")
        if e.get("missing") and status == "available":
            errors.append(f"{fid}: available fixtures must not list missing parts")
        if status == "planned":
            if e.get("items"):
                errors.append(f"{fid}: planned fixtures must not list items")
            continue
        if not e.get("license"):
            errors.append(f"{fid}: license/source required")
        golden = e.get("golden") or {}
        if golden.get("origin") not in ORIGINS:
            errors.append(f"{fid}: golden.origin must be one of {sorted(ORIGINS)} (never model output)")
        review = golden.get("review") or {}
        try:
            exp_items = _expected_items(e.get("expected"))
        except (OSError, ValueError) as exc:  # includes JSON errors and path escapes
            errors.append(f"{fid}: expected file unusable ({type(exc).__name__}: {exc})")
            exp_items = {}
        raw_items = e.get("items")
        errors.extend(_review_errors(fid, review, raw_items if isinstance(raw_items, list) else [],
                                     exp_items, today, verify_signoff))
        if status == "available" and (not isinstance(review, dict) or review.get("human") is not True):
            errors.append(f"{fid}: available requires a human review record")
        items = e.get("items") or []
        if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
            errors.append(f"{fid}: items must be a list of mappings")
            continue
        if not items:
            errors.append(f"{fid}: {status} requires items")
        for it in items:
            iid = it.get("id", "?")
            if iid in seen_items:
                errors.append(f"{iid}: duplicate item id")
            seen_items.add(iid)
            if not str(iid).startswith(f"{fid}."):
                errors.append(f"{iid}: item id must start with {fid}.")
            if iid not in exp_items:
                errors.append(f"{iid}: no expected entry")
            if not re.fullmatch(r"[0-9a-f]{64}", str(it.get("sha256", ""))):
                errors.append(f"{iid}: sha256 must be 64 lowercase hex")
                continue
            if ("generator" in it) == ("file" in it):
                errors.append(f"{iid}: exactly one of generator or file")
                continue
            if reproduce:
                try:
                    data = item_bytes(it)
                except Exception as exc:  # report, never crash the whole check
                    errors.append(f"{iid}: cannot reproduce ({type(exc).__name__}: {exc})")
                    continue
                if hashlib.sha256(data).hexdigest() != it["sha256"]:
                    errors.append(f"{iid}: sha256 mismatch (not reproducible)")
    return errors


def load() -> dict:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))


def main(argv=None, doc=None, fetch=None) -> int:
    """``--verify-signoff`` (used by scripts/check.sh) asks GitHub only for
    human reviews; with none recorded, no network is needed."""
    argv = sys.argv[1:] if argv is None else argv
    verify = github_signoff_verifier(fetch) if "--verify-signoff" in argv else None
    errors = check(load() if doc is None else doc, reproduce=True, verify_signoff=verify)
    for err in errors:
        print(f"fixtures: {err}", file=sys.stderr)
    if not errors:
        print("fixture registry ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
