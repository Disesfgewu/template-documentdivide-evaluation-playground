import json
import re
import unittest
from pathlib import Path

from lab_contracts import openapi

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md").read_text(encoding="utf-8")
DOC = json.loads((ROOT / "contracts" / "openapi" / "method-service.v1.json").read_text())


def refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


class OpenApiTests(unittest.TestCase):
    def test_paths_match_spec_7_2(self):
        section = SPEC[SPEC.index("### 7.2 HTTP 方法服務"):SPEC.index("prepare 請求包含")]
        spec_eps = {(m.lower(), p) for m, p in re.findall(r"^\| (GET|POST) \| (/v1/\S+) \|", section, re.M)}
        doc_eps = {(m, p) for p, ops in DOC["paths"].items() for m in ops}
        self.assertEqual(doc_eps, spec_eps)
        self.assertEqual(len(spec_eps), 5)

    def test_every_ref_resolves(self):
        names = set(DOC["components"]["schemas"])
        for r in refs(DOC):
            self.assertTrue(r.startswith("#/components/schemas/"), r)
            self.assertIn(r.rsplit("/", 1)[1], names)

    def test_generated_document_is_current(self):
        self.assertEqual(json.loads(json.dumps(openapi.build(), sort_keys=True)),
                         json.loads(json.dumps(DOC, sort_keys=True)))


if __name__ == "__main__":
    unittest.main()
