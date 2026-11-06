"""Validate fixtures/registry.yaml and reproduce every registered item.

Generator items are rebuilt in memory and must match the pinned sha256 byte
for byte; file items must exist and match. Only modules under
fixtures.generators may be used as generators (no arbitrary imports).
"""
import datetime
import hashlib
import importlib
import json
import re
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
    rf"https://github\.com/{re.escape(REPO)}/(issues|pull)/\d+"
    r"#(issuecomment|pullrequestreview|discussion_r)-?\d+")
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
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


def fixture_digest(items) -> str:
    """sha256 over the sorted "item_id:sha256" lines of one fixture version.
    A human review records this digest; any item change makes it stale."""
    lines = sorted(f"{it.get('id')}:{it.get('sha256')}" for it in items if isinstance(it, dict))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _review_errors(fid, review, items, today: datetime.date) -> list[str]:
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
        elif digest != fixture_digest(items):
            errors.append(f"{fid}: human review is stale (fixture_digest does not match the current items)")
    return errors


def check(doc: dict, reproduce: bool = True, today: datetime.date | None = None) -> list[str]:
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
        raw_items = e.get("items")
        errors.extend(_review_errors(fid, review, raw_items if isinstance(raw_items, list) else [], today))
        if status == "available" and (not isinstance(review, dict) or review.get("human") is not True):
            errors.append(f"{fid}: available requires a human review record")
        try:
            exp_items = _expected_items(e.get("expected"))
        except (OSError, ValueError) as exc:  # includes JSON errors and path escapes
            errors.append(f"{fid}: expected file unusable ({type(exc).__name__}: {exc})")
            exp_items = {}
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
