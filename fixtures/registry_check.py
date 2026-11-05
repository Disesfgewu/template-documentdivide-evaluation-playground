"""Validate fixtures/registry.yaml and reproduce every registered item.

Generator items are rebuilt in memory and must match the pinned sha256 byte
for byte; file items must exist and match. Only modules under
fixtures.generators may be used as generators (no arbitrary imports).
"""
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


def item_bytes(item: dict) -> bytes:
    if "generator" in item:
        name = item["generator"]
        if not re.fullmatch(r"fixtures\.generators\.[a-z_][a-z0-9_]*", str(name)):
            raise ValueError(f"generator {name!r} is outside fixtures.generators")
        return importlib.import_module(name).build(*item.get("args", []))
    path = ROOT / item["file"]
    if not path.resolve().is_relative_to(ROOT / "fixtures"):
        raise ValueError(f"file {item['file']!r} is outside fixtures/")
    return path.read_bytes()


def check(doc: dict, reproduce: bool = True) -> list[str]:
    errors = []
    entries = doc.get("fixtures") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return ["registry: missing fixtures list"]
    ids = [e.get("id") for e in entries]
    if ids != REQUIRED_IDS:
        errors.append(f"registry: fixtures must be exactly {REQUIRED_IDS} in order, got {ids}")
    seen_items = set()
    for e in entries:
        fid, status = e.get("id"), e.get("status")
        if status not in STATUSES:
            errors.append(f"{fid}: bad status {status!r}")
            continue
        if not e.get("missing") and status != "available":
            errors.append(f"{fid}: {status} must list what is missing")
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
        if not isinstance(review.get("human"), bool) or not review.get("by") or not review.get("date"):
            errors.append(f"{fid}: golden.review needs human (bool), by, date")
        if status == "available" and review.get("human") is not True:
            errors.append(f"{fid}: available requires a human review record")
        expected = e.get("expected")
        if not expected or not (ROOT / expected).is_file():
            errors.append(f"{fid}: expected file missing")
            exp_items = {}
        else:
            exp_items = json.loads((ROOT / expected).read_text(encoding="utf-8")).get("items", {})
        items = e.get("items") or []
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
