"""The lost-text guard against the REAL failure it exists for (Refs #7).

Runs only when ``LAB_E2E_READONLY_SITE=1``: the CI job makes the venv's site-packages read-only and the
spaCy model ``en_core_web_sm`` is absent, which is exactly what made the worker image return no text at all
(unstructured downloads that model on first use, fails to install it, logs one debug line and returns no
elements). The run must report the text-bearing pages as suspect and the document-level batch parse must turn that into
``text_extraction_empty`` instead of a clean, empty success. If the
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

from lab_parser.batch_parse import parse_in_batches
from lab_parser.isolation import InspectionError
from lab_parser.unstructured_runner import LOST_TEXT_DIAGNOSTIC, run_unstructured_fast
from deploy.unstructured_probe import synthetic_pdf


@unittest.skipUnless(os.environ.get("LAB_E2E_READONLY_SITE") == "1", "needs LAB_E2E_READONLY_SITE=1 (CI job e2e-parse)")
class LostTextGuardTests(unittest.TestCase):
    def test_the_guard_trips_when_the_spacy_model_cannot_be_installed(self):
        import unstructured
        site = Path(unstructured.__file__).resolve().parents[1]
        self.assertFalse(os.access(site, os.W_OK), "precondition: site-packages must be read-only for this test")
        self.assertIsNone(importlib.util.find_spec("en_core_web_sm"), "precondition: the spaCy model must be absent")
        # One run: no element, every page that has pypdf text is reported as suspect (needs_review + diagnostic).
        batch = run_unstructured_fast(synthetic_pdf(), document_id="doc-guard", budget_ref="b", operation_id="op-guard")
        self.assertEqual(len(batch.observations), 0)
        flagged = [p for p in batch.result.pages if LOST_TEXT_DIAGNOSTIC in p.diagnostics]
        self.assertTrue(flagged)
        self.assertTrue(all(p.status == "needs_review" for p in flagged))
        # The whole document: no element anywhere while text exists is a requeueable total loss, never a success.
        with self.assertRaises(InspectionError) as raised:
            parse_in_batches(synthetic_pdf(), document_id="doc-guard", budget_ref="b", operation_id="op-guard2")
        self.assertEqual(raised.exception.code, "text_extraction_empty")


if __name__ == "__main__":
    unittest.main()
