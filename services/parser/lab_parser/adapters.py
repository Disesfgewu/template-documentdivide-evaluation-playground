"""Parser adapter registry.

The SPEC requires two independent parsers (Unstructured, MinerU; Issue #8).
This skeleton only reports whether each backend is importable in this
environment. It never claims a parser is usable when it is not installed,
and it does not parse anything yet.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class Backend:
    name: str
    module: str
    distribution: str


BACKENDS = (
    Backend("unstructured", "unstructured", "unstructured"),
    Backend("mineru", "mineru", "mineru"),
)


def backend_status(backend: Backend) -> dict:
    if importlib.util.find_spec(backend.module) is None:
        return {"name": backend.name, "installed": False, "version": None,
                "adapter": "not_implemented"}
    try:
        version = importlib.metadata.version(backend.distribution)
    except importlib.metadata.PackageNotFoundError:
        version = None
    # Installed or not, no adapter exists until #8; never report "ready".
    return {"name": backend.name, "installed": True, "version": version,
            "adapter": "not_implemented"}


def capabilities() -> list[dict]:
    return [backend_status(b) for b in BACKENDS]
