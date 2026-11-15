"""Static governance checks for the repository.

These checks are deterministic and run on every PR (scripts/check.sh).
They exit non-zero on any violation so CI cannot be green on a broken
governance setup.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

REQUIRED_FILES = [
    "AGENTS.md",
    "docs/DEVELOPMENT.md",
    "docs/CODING_AGENT_LOOP.md",
    "docs/document-seperater-playground_SPEC_v0.1.md",
    ".github/pull_request_template.md",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/task.yml",
    ".github/ISSUE_TEMPLATE/bug.yml",
    "scripts/check.sh",
    "docs/TESTING.md",
]

PR_TEMPLATE_HEADINGS = [
    "## 問題與行為",
    "## Issue",
    "## SPEC 對應",
    "## 驗證",
    "## 資料與版本影響",
    "## Review修正（更新既有PR）",
    "## 合併與清理",
]

SECRET_PATTERNS = [
    ("github token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _load_yaml(path: Path, errors: list[str]):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path}: invalid YAML: {exc}")
        return None


def check_required_files(root: Path) -> list[str]:
    return [f"missing required file: {p}" for p in REQUIRED_FILES if not (root / p).is_file()]


def check_pr_template(root: Path) -> list[str]:
    path = root / ".github/pull_request_template.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    return [f"{path.name}: missing heading '{h}'" for h in PR_TEMPLATE_HEADINGS if h not in lines]


def check_issue_templates(root: Path) -> list[str]:
    errors: list[str] = []
    tdir = root / ".github/ISSUE_TEMPLATE"
    if not tdir.is_dir():
        return errors
    for path in sorted(tdir.glob("*.y*ml")):
        data = _load_yaml(path, errors)
        if data is None:
            continue
        if path.stem == "config":
            if not isinstance(data, dict):
                errors.append(f"{path}: config must be a mapping")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: issue form must be a mapping")
            continue
        for key in ("name", "description", "body"):
            if not data.get(key):
                errors.append(f"{path}: missing '{key}'")
        body = data.get("body") or []
        if not isinstance(body, list):
            errors.append(f"{path}: body must be a list")
            continue
        seen: set[str] = set()
        for i, item in enumerate(body):
            if not isinstance(item, dict) or "type" not in item:
                errors.append(f"{path}: body[{i}] missing type")
                continue
            if item["type"] == "markdown":
                continue
            ident = item.get("id")
            if not ident:
                errors.append(f"{path}: body[{i}] missing id")
            elif ident in seen:
                errors.append(f"{path}: duplicate id '{ident}'")
            else:
                seen.add(ident)
            if not (item.get("attributes") or {}).get("label"):
                errors.append(f"{path}: body[{i}] missing attributes.label")
    return errors


def check_workflows(root: Path) -> list[str]:
    """Every job must run at least one real command and must not ignore failure."""
    errors: list[str] = []
    wdir = root / ".github/workflows"
    files = sorted(wdir.glob("*.y*ml")) if wdir.is_dir() else []
    if not files:
        return ["no CI workflow found in .github/workflows"]
    for path in files:
        data = _load_yaml(path, errors)
        if not isinstance(data, dict):
            if data is not None:
                errors.append(f"{path}: workflow must be a mapping")
            continue
        jobs = data.get("jobs") or {}
        if not jobs:
            errors.append(f"{path}: no jobs")
        for name, job in jobs.items():
            if not isinstance(job, dict):
                errors.append(f"{path}: job {name} must be a mapping")
                continue
            if job.get("continue-on-error") is True:
                errors.append(f"{path}: job {name} sets continue-on-error")
            if "uses" in job:  # reusable workflow call
                continue
            steps = job.get("steps") or []
            runs = [s for s in steps if isinstance(s, dict) and str(s.get("run", "")).strip()]
            if not runs:
                errors.append(f"{path}: job {name} has no run step (empty green job)")
            for s in steps:
                if isinstance(s, dict) and s.get("continue-on-error") is True:
                    errors.append(f"{path}: job {name} step '{s.get('name', '?')}' sets continue-on-error")
                if isinstance(s, dict) and re.search(r"\|\|\s*true\b", str(s.get("run", ""))):
                    errors.append(f"{path}: job {name} step '{s.get('name', '?')}' masks failure with '|| true'")
    return errors


def check_codeowners(root: Path) -> list[str]:
    path = root / ".github/CODEOWNERS"
    ids_path = root / "tools/governance/verified_identities.txt"
    if not path.is_file():
        return []
    verified: set[str] = set()
    if ids_path.is_file():
        for line in ids_path.read_text(encoding="utf-8").splitlines():
            token = line.split("#", 1)[0].strip()
            if token:
                verified.add(token.lower())
    errors = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"CODEOWNERS:{n}: pattern without owner")
        for owner in parts[1:]:
            if not owner.startswith("@"):
                errors.append(f"CODEOWNERS:{n}: owner '{owner}' must be an @login")
            elif owner[1:].lower() not in verified:
                errors.append(f"CODEOWNERS:{n}: owner '{owner}' is not in verified_identities.txt")
    return errors


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, check=True, capture_output=True
        ).stdout.decode()
        return [root / p for p in out.split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def check_markdown_links(root: Path, files: list[Path]) -> list[str]:
    errors = []
    for path in files:
        if path.suffix != ".md" or not path.is_file():
            continue
        for target in MD_LINK.findall(path.read_text(encoding="utf-8")):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            rel = target.split("#", 1)[0]
            if rel and not (path.parent / rel).exists():
                errors.append(f"{path.relative_to(root)}: broken relative link '{target}'")
    return errors


def check_secrets(root: Path, files: list[Path]) -> list[str]:
    errors = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"{path.relative_to(root)}: possible {label} committed")
    return errors


FORBIDDEN_SUFFIXES = (".pem", ".key")
ALLOWED_ENV_TEMPLATES = (".example",)


def check_forbidden_files(root: Path, files: list[Path]) -> list[str]:
    """Credential-shaped files must never be tracked: ``.env``/``.env.*`` (only ``*.example`` templates are
    allowed) and ``*.pem``/``*.key``. The file names are reported, never their contents."""
    errors = []
    for path in files:
        name = path.name
        is_env = name == ".env" or (name.startswith(".env.") and not name.endswith(ALLOWED_ENV_TEMPLATES))
        if is_env or name.endswith(FORBIDDEN_SUFFIXES):
            errors.append(f"{path.relative_to(root)}: credential-shaped file is tracked (only *.example templates are allowed)")
    return errors


def run_all(root: Path) -> list[str]:
    files = tracked_files(root)
    errors: list[str] = []
    errors += check_required_files(root)
    errors += check_pr_template(root)
    errors += check_issue_templates(root)
    errors += check_workflows(root)
    errors += check_codeowners(root)
    errors += check_markdown_links(root, files)
    errors += check_secrets(root, files)
    errors += check_forbidden_files(root, files)
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", type=Path)
    args = ap.parse_args(argv)
    errors = run_all(args.root.resolve())
    for e in errors:
        print(f"GOVERNANCE FAIL: {e}", file=sys.stderr)
    print(f"governance checks: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
