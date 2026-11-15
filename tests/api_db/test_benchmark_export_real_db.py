"""The owner can SEE and DOWNLOAD what was generated (Issue #186 part 2), against real PostgreSQL with the fake lab-gen."""
from __future__ import annotations

import json
import unittest
import uuid

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_api.generate_worker_main import GenerateWorker
from lab_db.storage import LocalArtifactStore

BASE = "/api/v1/benchmarks/bench-gen/revisions/1"
BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
KEY = "sk-ant-secret-export-test-key"


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class BenchmarkExportRealDbTests(GenerateApiFixtures, FreshDatabase):
    def validated(self, validation="validate_mixed"):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        for mode in ("questions",):
            self.run_worker_with_key(mode)
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker_with_key(validation)
        return project, owner

    def run_worker_with_key(self, mode):
        return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key=KEY, name="export-test", interval=1, backoff=1, grace=5,
                              once_timeout=120, store=LocalArtifactStore(self.root), extra_env={"FAKE_LAB_GEN_MODE": mode}).run_once()

    def get(self, who, project, path):
        return self.client.get(f"{BASE}/{path}{'&' if '?' in path else '?'}project_id={project}", headers=self.headers(who))

    def test_the_preview_has_everything_the_owner_needs_to_check_a_question(self):
        project, owner = self.validated()
        res = self.get(owner, project, "questions/review")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertIn("answered from that text alone", body["validation_explained"])
        self.assertEqual(body["total"], 18)
        buckets = {i["bucket"] for i in body["items"]}
        self.assertEqual(buckets, {"general", "cross_page", "confusable"})
        for item in body["items"]:
            self.assertTrue(item["question_text"] and item["reference_answer"] and item["required_facts"], item["ordinal"])
            self.assertTrue(all(f["statement"] and f["weight"] for f in item["required_facts"]))
            self.assertTrue(item["evidence_sets"] and item["evidence_sets"][0]["units"])
            for unit in item["evidence_sets"][0]["units"]:
                self.assertTrue(unit["text"] and unit["pages"] and all(p >= 1 for p in unit["pages"]), unit)       # 1-based pages, real text
            self.assertIn(item["validation"]["verdict"], ("pass", "fail", "needs_review"))
            self.assertTrue(item["validation"]["reason"])
            self.assertIn("passed", item["validation"]["code_checks"])
            self.assertEqual(item["verdict"], item["validation"]["verdict"])                                # the flat fields the page already used
        confusable = [i for i in body["items"] if i["bucket"] == "confusable"]
        self.assertTrue(confusable and all(d["confusion_reason"] and d["text"] for i in confusable for d in i["distractors"]))
        cross = [i for i in body["items"] if i["bucket"] == "cross_page"]
        self.assertTrue(all(len({p for u in i["evidence_sets"][0]["units"] for p in u["pages"]}) >= 2 for i in cross))       # it really spans pages

    def test_the_json_download_is_the_whole_benchmark_with_a_header_and_no_key(self):
        project, owner = self.validated()
        qid = self.get(owner, project, "questions/review").json()["items"][0]["id"]
        self.assertEqual(self.api("post", owner, f"{BASE}/questions/{qid}/exclusion", project, {"excluded": True, "reason": "too vague"}).status_code, 200)
        res = self.get(owner, project, "export?format=json")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers["content-type"].split(";")[0], "application/json")
        self.assertRegex(res.headers["content-disposition"], r'^attachment; filename="bench-gen-r1\.json"$')
        self.assertEqual(res.headers["cache-control"], "no-store")
        data = json.loads(res.text)
        self.assertEqual((data["schema"], data["logical_id"], data["revision"], data["contains_gold"], data["human_review"]),
                         ("lab-benchmark-export-1", "bench-gen", 1, True, "skipped"))
        self.assertEqual((data["questions"], data["excluded"], len(data["items"])), (18, 1, 18))
        self.assertEqual(len(data["document_sha256"]), 64)
        excluded = [i for i in data["items"] if i["excluded"]]
        self.assertEqual([(i["id"], i["exclusion_reason"]) for i in excluded], [(qid, "too vague")])               # recorded, still in the file
        self.assertTrue(any("gold" in n for n in data["notes"]))
        for secret in (KEY, "sk-ant", "Authorization", "api_key", "request_sha256"):
            self.assertNotIn(secret, res.text)

    def test_the_jsonl_download_has_the_header_then_one_question_per_line(self):
        project, owner = self.validated()
        res = self.get(owner, project, "export?format=jsonl")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers["content-type"].split(";")[0], "application/x-ndjson")
        self.assertIn('filename="bench-gen-r1.jsonl"', res.headers["content-disposition"])
        lines = res.text.splitlines()
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["type"], "benchmark")
        self.assertTrue(all(p["type"] == "question" for p in parsed[1:]))
        self.assertEqual(len(parsed) - 1, parsed[0]["questions"])
        as_json = json.loads(self.get(owner, project, "export?format=json").text)
        self.assertEqual([p["id"] for p in parsed[1:]], [i["id"] for i in as_json["items"]])
        self.assertNotIn(KEY, res.text)

    def test_only_owner_or_maintainer_may_download_and_the_format_is_checked(self):
        project, owner = self.validated("validate_ok")
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            participant = self.add_member(c, project, "participant")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        self.assertEqual(self.get(maintainer, project, "export").status_code, 200)
        self.assertEqual(self.get(participant, project, "export").status_code, 403)
        self.assertEqual(self.get(outsider, project, "export").status_code, 404)
        self.assertEqual(self.get(owner, project, "export?format=csv").status_code, 400)
        self.assertEqual(self.client.get(f"{BASE}/export?project_id={project}").status_code, 401)                 # no token: no gold

    def test_the_download_works_after_the_freeze_too(self):
        project, owner = self.validated("validate_ok")
        self.assertEqual(self.api("post", owner, f"{BASE}/freeze", project, {"compliance": "partial"}).status_code, 200)
        data = json.loads(self.get(owner, project, "export").text)
        self.assertEqual((data["status"], data["compliance"], data["excluded"]), ("frozen", "partial", 0))
        self.assertEqual(len(data["items"]), 18)


if __name__ == "__main__":
    unittest.main()
