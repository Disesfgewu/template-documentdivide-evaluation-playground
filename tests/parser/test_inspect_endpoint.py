"""POST /v1/inspect: import outcome and page geometry over HTTP, using the
F02/F05 fixtures. Rejections are a successful inspection (HTTP 200)."""
import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from fixtures import registry_check as rc
from lab_parser.importer import Limits
from lab_parser.main import create_app
from tests.parser.test_parser_service import settings

DOC = rc.load()["fixtures"]
F02 = next(e for e in DOC if e["id"] == "F02")
F05 = {it["id"]: it for it in next(e for e in DOC if e["id"] == "F05")["items"]}
F05_EXPECTED = json.loads((rc.ROOT / "fixtures" / "F05" / "expected.json").read_text())["items"]


class InspectEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(create_app(settings(self.tmp.name)))

    def post(self, data, ctype="application/pdf", client=None):
        return (client or self.client).post("/v1/inspect", content=data, headers={"content-type": ctype})

    def test_f02_pages_and_geometry(self):
        r = self.post(rc.item_bytes(F02["items"][0]))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["inspection"]["outcome"], "accept")
        self.assertEqual([p["rotation"] for p in body["pages"]], [0, 90, 180, 270, 0, 90])
        self.assertEqual(body["pages"][4]["display_width_pt"], 540.0)

    def test_f05_outcomes_match_expected(self):
        mapping = {"reject": "reject", "flag": "flag", "accept_as_data": "accept", "reject_if_over_limit": "reject"}
        for item_id in ("F05.empty", "F05.fake_text", "F05.truncated", "F05.encrypted", "F05.bad_xref", "F05.injection"):
            want = F05_EXPECTED[item_id]
            with self.subTest(item_id):
                r = self.post(rc.item_bytes(F05[item_id]))
                self.assertEqual(r.status_code, 200)
                got = r.json()["inspection"]
                self.assertEqual((got["outcome"], got["reason"]), (mapping[want["outcome"]], want["reason"]))
                if got["outcome"] == "reject":
                    self.assertEqual(r.json()["pages"], [])

    def test_wrong_media_type_is_415(self):
        self.assertEqual(self.post(b"%PDF-1.4", ctype="text/plain").status_code, 415)
        self.assertEqual(self.post(b"%PDF-1.4", ctype="").status_code, 415)

    def test_over_limit_is_413(self):
        small = TestClient(create_app(settings(self.tmp.name), limits=Limits(max_bytes=100)))
        r = self.post(rc.item_bytes(F02["items"][0]), client=small)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"]["code"], "payload_too_large")

    def test_no_parse_endpoint_still(self):
        self.assertEqual(self.client.post("/v1/parse").status_code, 404)


if __name__ == "__main__":
    unittest.main()
