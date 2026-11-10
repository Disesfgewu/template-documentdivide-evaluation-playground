"""The lost-text guard against the REAL failure it exists for (Refs #7).

Runs only when ``LAB_E2E_READONLY_SITE=1``: the CI job makes the venv's site-packages read-only and the
spaCy model ``en_core_web_sm`` is absent, which is exactly what made the worker image return no text at all
(unstructured downloads that model on first use, fails to install it, logs one debug line and returns no
elements). The guard must turn that into ``text_extraction_empty`` instead of a clean, empty success. If the
precondition does not hold (model present or directory writable) the test FAILS rather than passing vacuously.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_parser.isolation import InspectionError
from lab_parser.unstructured_runner import run_unstructured_fast
from deploy.unstructured_probe import synthetic_pdf


@unittest.skipUnless(os.environ.get("LAB_E2E_READONLY_SITE") == "1", "needs LAB_E2E_READONLY_SITE=1 (CI job e2e-parse)")
class LostTextGuardTests(unittest.TestCase):
    def test_the_guard_trips_when_the_spacy_model_cannot_be_installed(self):
        import unstructured
        site = Path(unstructured.__file__).resolve().parents[1]
        self.assertFalse(os.access(site, os.W_OK), "precondition: site-packages must be read-only for this test")
        self.assertIsNone(importlib.util.find_spec("en_core_web_sm"), "precondition: the spaCy model must be absent")
        with self.assertRaises(InspectionError) as raised:
            run_unstructured_fast(synthetic_pdf(), document_id="doc-guard", budget_ref="b", operation_id="op-guard")
        self.assertEqual(raised.exception.code, "text_extraction_empty")


if __name__ == "__main__":
    unittest.main()
