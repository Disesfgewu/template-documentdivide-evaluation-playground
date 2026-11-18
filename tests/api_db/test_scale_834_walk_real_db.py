"""Walk the whole flow at 834 pages with the fake model (PM-DECISION on #204, item 2): parse result -> plan/promote -> estimate -> generate (a small
question_count) -> validate -> review -> freeze -> questions-only export -> developer upload (quote location) -> rule scoring -> judge -> results.
It PRINTS a table of time and payload size per step and asserts only generous bounds. Opt-in (LAB_SCALE=1): it takes minutes, and CI counts a skipped test as
a failure, so the class is not defined otherwise."""
from __future__ import annotations

import json
import os
import resource
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

if os.environ.get("LAB_SCALE") == "1":
    from lab_api.generate_worker_main import GenerateWorker
    from lab_api.parse_results import record_from_batch
    from lab_contracts.parse import ModelRevision, PageMapEntry, ParseManifest, ParseRequest, RenderSettings
    from lab_db import jobs
    from lab_db.parse_results import persist_parse_result
    from lab_db.storage import LocalArtifactStore
    from lab_parser.batch_parse import parse_in_batches
    from lab_parser.normalize import ADAPTER_REVISION, DisplayFrame, UnstructuredParser, UnstructuredProfile
    from fixtures.generators import synthetic_pdf
    from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB

    PAGES = int(os.environ.get("LAB_SCALE_PAGES", "834"))
    PER_PAGE = int(os.environ.get("LAB_SCALE_UNITS", "94"))
    BASE = "/api/v1/benchmarks/bench-gen/revisions/1"
    TEXT = ("The memory controller redirects a write to a location whose content is already known so that fewer cells are programmed. " * 3)[:330]

    def big_batch(page_indices, document_id, sha, operation_id, budget_ref):
        """What the parser child returns for a set of pages of a PAGES-page document: PER_PAGE elements per page, one distinct sentence each."""
        now = datetime.now(timezone.utc)
        elements = []
        for k, p in enumerate(page_indices):
            for j in range(PER_PAGE):
                y0 = 100 + j * 6.5
                elements.append({"type": "NarrativeText", "element_id": f"e{p}-{j}", "text": f"Section {p}.{j} says: {TEXT} (code {p * 1000 + j})", "metadata": {
                    "page_number": k + 1, "coordinates": {"system": "PixelSpace", "layout_width": 595, "layout_height": 842,
                                                           "points": [[61.2, y0], [61.2, y0 + 6], [306, y0 + 6], [306, y0]]}}})
        request = ParseRequest(document_artifact_ref="sha256:" + sha, page_indices=list(page_indices), profile_revision="unstructured-oss-fast",
                               budget_ref=budget_ref, operation_id=operation_id)
        manifest = ParseManifest(
            parser_name="unstructured", package_or_service_version="0.27.36", adapter_revision=ADAPTER_REVISION, output_schema="unstructured.elements-0.27.36",
            model_revisions=[ModelRevision(name="core", revision="0.27.36", checksum=None)], requested_profile="unstructured-oss-fast",
            effective_profile="unstructured-oss-fast", fallback_reason=None, ocr_languages=["en"], page_selection=list(page_indices),
            page_index_map=[PageMapEntry(parser_page=k + 1, page_index=p) for k, p in enumerate(page_indices)],
            render_settings=RenderSettings(dpi=150, max_megapixels=16.0), postprocess_flags=[], endpoint_deployment_id=None, started_at=now, finished_at=now,
            cost_usd=0.0, warnings=[], reproducible=True)
        frames = {p: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=595.0, height=842.0, transform_ref=f"tr-{p}") for p in page_indices}
        return UnstructuredParser().normalize(json.dumps(elements).encode(), request, manifest, UnstructuredProfile(strategy="fast"), document_id=document_id,
                                              document_sha256=sha, frames=frames, document_page_count=PAGES)

    def rss_mib():
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    @unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
    class Scale834WalkTests(GenerateApiFixtures, FreshDatabase):
        def step(self, name, fn):
            t = time.monotonic()
            out = fn()
            self.rows.append((name, time.monotonic() - t, out))
            return out

        def worker(self, mode, capture=None):
            env = {"FAKE_LAB_GEN_MODE": mode}
            if capture:
                env["FAKE_LAB_GEN_CAPTURE"] = capture
            return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="scale", interval=1, backoff=1, grace=5, once_timeout=900,
                                  store=LocalArtifactStore(self.root), extra_env=env)

        def size_of(self, res):
            self.assertEqual(res.status_code, 200, res.text[:300])
            return f"{len(res.content) / 1024:.1f} KiB"

        def test_the_whole_flow_at_834_pages(self):
            self.rows = []
            with self.connect() as c:
                project, owner = self.make_project(c)
            pdf = synthetic_pdf.build(PAGES)
            up = self.step("upload a %d-page PDF" % PAGES, lambda: self.client.post(
                f"/api/v1/documents?project_id={project}&logical_id=scale", headers=self.headers(owner, **{"Content-Type": "application/pdf"}), content=pdf))
            self.assertEqual(up.status_code, 201, up.text)
            doc, sha = uuid.UUID(up.json()["id"]), up.json()["sha256"]
            job_res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json={})
            job = uuid.UUID(job_res.json()["job_id"])
            with self.connect() as c:
                op = c.execute("SELECT operation_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()[0]

            merged = self.step("parse (synthetic parser, %d elements/page)" % PER_PAGE, lambda: parse_in_batches(
                pdf, document_id=str(doc), budget_ref="b", operation_id=str(op), run_batch=lambda data, **kw: big_batch(kw["page_indices"], str(doc), sha, kw["operation_id"], kw["budget_ref"])))
            record = record_from_batch(merged)

            def persist():
                with self.connect() as c:
                    lease = jobs.acquire(c, project, job, "w1")
                    jobs.finish(c, project, lease, "succeeded", "parsed", hook=lambda conn: persist_parse_result(conn, self.store, project, lease, record))
            self.step("store the parse result (%d units)" % len(merged.evidence), persist)

            res = self.step("POST benchmark", lambda: self.api("post", owner, "/api/v1/benchmarks", project, {"document_version_id": str(doc), "logical_id": "bench-gen"}))
            self.assertEqual(res.status_code, 201, res.text)
            res = self.step("POST plan (promote the parse: write all evidence units)", lambda: self.api("post", owner, f"{BASE}/plan", project))
            self.assertEqual(res.status_code, 200, res.text)
            self.rows[-1] = (self.rows[-1][0], self.rows[-1][1], f"{len(res.content) / 1024:.1f} KiB")
            self.step("GET plan", lambda: self.size_of(self.api("get", owner, f"{BASE}/plan", project)))
            self.step("GET progress (generate status)", lambda: self.size_of(self.api("get", owner, f"{BASE}/generate", project)))
            self.step("GET workspace", lambda: self.size_of(self.client.get(f"/api/v1/workspace?project_id={project}", headers=self.headers(owner))))
            self.step("GET estimate generate, 30 questions", lambda: self.size_of(self.api("get", owner, f"{BASE}/estimate?phase=generate&question_count=30", project).__class__ and
                                                                                    self.client.get(f"{BASE}/estimate?project_id={project}&phase=generate&question_count=30", headers=self.headers(owner))))

            import tempfile
            cap = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
            cap.close()
            self.addCleanup(os.unlink, cap.name)
            res = self.step("POST generate (question_count=30)", lambda: self.api("post", owner, f"{BASE}/generate", project, {"model": "fake-model", "question_count": 30}))
            self.assertEqual(res.status_code, 202, res.text)
            self.step("worker: generate (fake model, 30 questions)", lambda: self.worker("questions", cap.name).run_once())
            with open(cap.name, encoding="utf-8") as fh:
                prompts = [json.loads(x) for x in fh.read().splitlines() if x.strip()]
            pages_used = sorted({int(p["id"].rsplit("-p", 1)[1]) for p in prompts if "-p" in p["id"] and not p["id"].startswith(("x", "c"))})
            print(f"\nGENERATE prompts: {len(prompts)}; general-question pages ({len(pages_used)}): first {pages_used[:8]} ... last {pages_used[-5:]}")
            if PAGES >= 200:
                self.assertLess(min(pages_used), PAGES * 0.1)                              # spread over the whole document, not its first pages
                self.assertGreater(max(pages_used), PAGES * 0.8)
            print(f"GENERATE prompt bytes: max {max(len(p['user']) for p in prompts)}, sum {sum(len(p['user']) for p in prompts)}")

            est = self.client.get(f"{BASE}/estimate?project_id={project}&phase=validate", headers=self.headers(owner))
            print("\nVALIDATE estimate:", est.status_code, est.text[:400])
            with self.connect() as c:
                print("QUESTIONS written:", c.execute("SELECT quota_bucket, count(*) FROM questions GROUP BY 1").fetchall())
            res = self.step("POST validate", lambda: self.api("post", owner, f"{BASE}/validate", project, {"model": "fake-model"}))
            self.assertEqual(res.status_code, 202, res.text)
            self.step("worker: validate (fake model)", lambda: self.worker("validate_ok").run_once())
            self.step("GET review", lambda: self.size_of(self.api("get", owner, f"{BASE}/questions/review", project)))
            res = self.step("POST freeze", lambda: self.api("post", owner, f"{BASE}/freeze", project, {"compliance": "partial"}))
            self.assertEqual(res.status_code, 200, res.text)
            self.step("GET questions-only export", lambda: self.size_of(self.api("get", owner, f"{BASE}/questions/export", project)))

            review = self.api("get", owner, f"{BASE}/questions/review", project).json()
            gold = {i["question_key"]: i for i in review["items"]}
            lines = ['{"method_name": "scale-method", "method_version": "1"}']
            for key, item in sorted(gold.items()):
                unit = item["evidence_sets"][0]["units"][0]
                lines.append(json.dumps({"question_id": key, "status": "answered", "answer": item["reference_answer"],
                                         "sources": [{"page": unit["pages"][0], "text": unit["text"]}]}))
            content = "\n".join(lines) + "\n"
            res = self.step("POST developer upload (%d answers, quote location in %d pages)" % (len(gold), PAGES), lambda: self.client.post(
                f"{BASE}/submissions?project_id={project}", headers=self.headers(owner), json={"method_name": "scale-method", "file_content": content}))
            self.assertEqual(res.status_code, 201, res.text[:300])
            run = res.json()["run_id"]
            self.rows[-1] = (self.rows[-1][0], self.rows[-1][1], f"found {res.json()['report']['source_checks']}")
            self.step("GET results", lambda: self.size_of(self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner))))
            self.step("GET submission-details", lambda: self.size_of(self.api("get", owner, f"/api/v1/runs/{run}/submission-details", project)))
            est = self.client.get(f"{BASE}/estimate?project_id={project}&phase=judge&run_id={run}", headers=self.headers(owner))
            print("\nJUDGE estimate (derived cap):", est.status_code, est.text[:260])
            res = self.step("POST judge-runs", lambda: self.api("post", owner, f"/api/v1/runs/{run}/judge-runs", project, {"model": "fake-model", "budget_cap_micros": 50_000_000}))
            self.assertEqual(res.status_code, 202, res.text)
            self.step("worker: judge (fake model)", lambda: self.worker("judge_ok").run_once())
            self.step("GET results after judge", lambda: self.size_of(self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner))))
            print(f"\nWALK {PAGES} pages x {PER_PAGE} units (peak test-process RSS {rss_mib():.0f} MiB)")
            for name, secs, out in self.rows:
                print(f"  {secs:8.2f}s  {name}   {out if isinstance(out, str) else ''}")
            with self.connect() as c:
                print("  rows:", {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                                  ("evidence_units", "evidence_regions", "evidence_unit_sources", "questions", "evidence_sets", "evidence_set_members")})

if __name__ == "__main__":
    unittest.main()
