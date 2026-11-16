"""Questions-only export, uploaded developer answers, quote checks, scoring, judging and comparison (Issue #189), real PostgreSQL."""
from __future__ import annotations

import json
import unittest
import uuid

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_api import benchmark_external
from lab_api.generate_worker_main import GenerateWorker
from lab_db.storage import LocalArtifactStore

BASE = "/api/v1/benchmarks/bench-gen/revisions/1"
BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
FORBIDDEN = ("gold_answers", "facts", "evidence_units", "evidence_regions", "evidence_sets", "distractors", "question_validations",
             "question_exclusions", "parse_page_results", "parse_results", "benchmark_parse_sources", "evidence_unit_sources")


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ExternalSubmissionRealDbTests(GenerateApiFixtures, FreshDatabase):
    def worker(self, mode):
        return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="ext-test", interval=1, backoff=1, grace=5,
                              once_timeout=120, store=LocalArtifactStore(self.root), extra_env={"FAKE_LAB_GEN_MODE": mode})

    def frozen(self, legacy_gold=False):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.worker("questions").run_once()
        self.api("post", owner, self.VAL, project, BODY)
        self.worker("validate_ok").run_once()
        if legacy_gold:          # a benchmark generated before #197: no gold_granularity marker, so its gold is page-level (frozen ones cannot be edited)
            with self.connect() as c:
                c.execute("UPDATE questions SET payload = payload - 'gold_granularity'")
                c.commit()
        self.assertEqual(self.api("post", owner, f"{BASE}/freeze", project, {"compliance": "partial"}).status_code, 200)
        return project, owner

    def get(self, who, project, path):
        return self.client.get(f"{path}{'&' if '?' in path else '?'}project_id={project}", headers=self.headers(who))

    def gold(self, owner, project):
        """question_key -> (right page 1-based, the real passage text) from the owner's review (gold side)."""
        out = {}
        for item in self.get(owner, project, f"{BASE}/questions/review").json()["items"]:
            unit = item["evidence_sets"][0]["units"][0]
            out[item["question_key"]] = (unit["pages"][0], unit["text"], item["reference_answer"])
        return out

    def file(self, owner, project, *, fabricate=(), abstain=(), skip=(), wrong_page=()):
        gold = self.gold(owner, project)
        lines = ['{"method_name": "file-method", "method_version": "1"}']
        for i, (key, (page, text, answer)) in enumerate(sorted(gold.items())):
            if key in skip or i in skip:
                continue
            if key in abstain or i in abstain:
                lines.append(json.dumps({"question_id": key, "status": "abstained", "reason": "not found"}))
                continue
            src = {"page": page, "text": text}
            if i in fabricate:
                src = {"page": page, "text": "An entirely invented sentence about fees and deadlines"}
            if i in wrong_page:
                src = {"page": 2, "text": text}                        # page 2 has no text in the fixture
            lines.append(json.dumps({"question_id": key, "status": "answered", "answer": answer, "sources": [src]}))
        return "\n".join(lines) + "\n"

    def upload(self, who, project, content, **extra):
        body = {"method_name": "file-method", "file_content": content, **extra}
        return self.client.post(f"{BASE}/submissions?project_id={project}", headers=self.headers(who), json=body)

    # --- the participant package ----------------------------------------------------------------------------------

    def test_the_questions_package_has_only_ids_text_and_language_and_the_query_never_touches_gold(self):
        project, owner = self.frozen()
        res = self.get(owner, project, f"{BASE}/questions/export?format=jsonl")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn('filename="bench-gen-r1-questions.jsonl"', res.headers["content-disposition"])
        rows = [json.loads(x) for x in res.text.splitlines()]
        head, questions = rows[0], rows[1:]
        self.assertEqual((head["type"], head["schema"], head["contains_gold"], head["revision"], head["questions"]), ("benchmark", "lab-questions-package-1", False, 1, 18))
        self.assertEqual((len(head["document_sha256"]), head["document_page_count"], head["answers_file_format_url"]), (64, 3, "/api/v1/submission-format"))
        self.assertEqual(len(questions), 18)
        for q in questions:
            self.assertEqual(set(q), {"type", "question_id", "ordinal", "question_text", "language"})
        text = res.text.lower()
        for word in ("reference_answer", "required_facts", "evidence", "bucket", "verdict", "distractor", "cross_page", "confusable", "weight"):
            self.assertNotIn(word, text, word)
        as_json = json.loads(self.get(owner, project, f"{BASE}/questions/export?format=json").text)
        self.assertEqual([q["question_id"] for q in as_json["items"]], [q["question_id"] for q in questions])

        executed = []

        class Rec:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *a, **k):
                executed.append(str(sql))
                return self.conn.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self.conn, name)
        with self.connect() as c:
            benchmark_external._questions_package(Rec(c), owner, project, "bench-gen", 1)
        text = " ".join(executed).lower()
        for table in FORBIDDEN:
            self.assertNotIn(table, text, table)                           # SQL capture: no gold or construction table is read
        self.assertIn("active_questions", text)
        self.assertNotIn("payload,", text.replace("payload ->", "payload->"))     # the stored question payload (it names the bucket) is not selected

    def test_the_package_needs_a_frozen_benchmark_and_an_owner_or_maintainer(self):
        project, owner = self.planned()
        self.assertEqual(self.get(owner, project, f"{BASE}/questions/export").status_code, 409)
        project, owner = self.frozen()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            participant = self.add_member(c, project, "participant")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        self.assertEqual(self.get(maintainer, project, f"{BASE}/questions/export").status_code, 200)
        self.assertEqual(self.get(participant, project, f"{BASE}/questions/export").status_code, 403)
        self.assertEqual(self.get(outsider, project, f"{BASE}/questions/export").status_code, 404)
        self.assertEqual(self.get(owner, project, f"{BASE}/questions/export?format=csv").status_code, 400)
        doc = self.get(owner, project, "/api/v1/submission-format").json()
        self.assertEqual((doc["format"], len(doc["example_lines"])), ("lab-submission-1", 3))

    # --- upload and scoring ------------------------------------------------------------------------------------------------

    def test_the_whole_flow_export_to_answers_to_scores_with_fabricated_missing_and_abstained_questions(self):
        project, owner = self.frozen()
        content = self.file(owner, project, fabricate={0}, abstain={1}, skip={2}, wrong_page={3})
        res = self.upload(owner, project, content)
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        rep = body["report"]
        self.assertEqual((rep["answered"], rep["abstained"], rep["not_submitted"], rep["problem_count"]), (16, 1, 1, 0))
        self.assertEqual(rep["source_checks"], {"total": 16, "found": 14, "not_found": 1, "page_without_text": 1})
        self.assertEqual((body["status"], body["evaluation_status"], body["method_name"]), ("completed_with_errors", "provisional", "file-method"))
        run = body["run_id"]
        results = self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner)).json()
        self.assertEqual(results["submissions"], {"answered": 16, "abstained_by_model": 1, "no_answer_retrieval_only": 0, "method_failures": {"not_submitted": 1}})
        metrics = {m["metric_name"]: m for m in results["metrics"]}
        self.assertEqual(metrics["recall_at_k"]["scored"], 18)                  # retrieval is scored for every question; the missing one scores 0
        self.assertGreater(metrics["recall_at_k"]["mean"], 70)
        self.assertEqual((metrics["correctness"]["error"], metrics["correctness"]["pending"]), (1, 17))      # not_submitted is an error, the rest wait for the judge
        self.assertEqual(results["evaluation"]["grader_revision"], "rule-based-scorer-1")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT m.execution_mode FROM method_revisions m JOIN runs r ON r.method_revision_id = m.id WHERE r.id = %s", (run,)).fetchone()[0], "offline_file")
            self.assertEqual(c.execute("SELECT DISTINCT transport FROM attempts WHERE run_id = %s", (run,)).fetchall(), [("offline_file",)])
            sha, artifact = c.execute("SELECT file_sha256, file_artifact_id FROM external_submissions WHERE run_id = %s", (run,)).fetchone()
            import hashlib
            self.assertEqual(sha, hashlib.sha256(content.encode()).hexdigest())
            self.assertEqual(c.execute("SELECT sha256 FROM artifacts WHERE id = %s", (artifact,)).fetchone()[0], sha)
            self.assertEqual(c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND failure_reason = 'not_submitted'", (run,)).fetchone()[0], 1)
        detail = self.get(owner, project, f"/api/v1/runs/{run}/submission-details").json()
        self.assertTrue(detail["contains_gold"])
        by_ord = {i["ordinal"]: i for i in detail["items"]}
        self.assertEqual(len(by_ord), 18)
        sources = [s for i in detail["items"] for s in i["their_sources"]]
        self.assertEqual({s["match_quality"] for s in sources}, {"exact_in_unit", "not_found", "page_without_text"})
        fake = [s for s in sources if s["match_quality"] == "not_found"][0]
        self.assertEqual((fake["text_found"], fake["region_kind"]), (False, "page"))
        self.assertLess(fake["quote_coverage"], 1)
        real = [s for s in sources if s["match_quality"] == "exact_in_unit"][0]
        self.assertEqual((real["text_found"], real["quote_coverage"]), (True, 1.0))
        missing = [i for i in detail["items"] if i["failure_reason"] == "not_submitted"][0]
        self.assertEqual((missing["status"], missing["their_answer"], missing["their_sources"]), ("abstained", None, []))
        self.assertTrue(all(i["gold"]["reference_answer"] and i["gold"]["required_facts"] for i in detail["items"]))
        self.assertEqual(missing["scores"]["correctness"]["reason_codes"], ["not_submitted"])

    def test_the_judge_scores_an_uploaded_file_from_its_answer_and_its_submitted_passages(self):
        project, owner = self.frozen()
        run = self.upload(owner, project, self.file(owner, project, abstain={0}, skip={1})).json()["run_id"]
        j = self.client.post(f"/api/v1/runs/{run}/judge-runs?project_id={project}", headers=self.headers(owner), json={"model": "fake-model", "budget_cap_micros": 50_000_000})
        self.assertEqual(j.status_code, 202, j.text)
        self.assertEqual(j.json()["answered_submissions"], 16)
        self.worker("judge_ok").run_once()
        results = self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner)).json()
        self.assertEqual(results["evaluation"]["grader_revision"], "rule-based-scorer-1+llm-judge-1")
        self.assertEqual(results["judge"]["judged_questions"], 16)
        self.assertIn("same provider/model family", results["judge"]["limitation"])
        metrics = {m["metric_name"]: m for m in results["metrics"]}
        self.assertEqual((metrics["faithfulness"]["scored"], metrics["faithfulness"]["not_applicable"], metrics["faithfulness"]["error"]), (16, 1, 1))
        self.assertEqual(metrics["correctness"]["mean"], metrics["correctness"]["mean"])
        est = self.get(owner, project, f"{BASE}/estimate?phase=judge&run_id={run}")
        self.assertEqual(est.status_code, 200, est.text)

    # --- the judge never sees a quote that was not found, and an invented source earns no judged credit -----------------------------

    FAKE = "ZZQ an invented sentence about cheese and tax law"

    def judged_file(self, owner, project, sources_for_first):
        """Question 0 answered with the given sources (a function of its gold page and text); every other question abstains."""
        gold = self.gold(owner, project)
        keys = sorted(gold)
        page, text, answer = gold[keys[0]]
        lines = [json.dumps({"question_id": keys[0], "status": "answered", "answer": answer, "sources": sources_for_first(page, text)})]
        lines += [json.dumps({"question_id": k, "status": "abstained", "reason": "skip"}) for k in keys[1:]]
        return "\n".join(lines) + "\n", keys[0], text

    def judge_with_capture(self, owner, project, run, mode="judge_ok"):
        import os
        import tempfile
        cap = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        cap.close()
        self.addCleanup(os.unlink, cap.name)
        j = self.client.post(f"/api/v1/runs/{run}/judge-runs?project_id={project}", headers=self.headers(owner), json={"model": "fake-model", "budget_cap_micros": 50_000_000})
        self.assertEqual(j.status_code, 202, j.text)
        w = self.worker(mode)
        w.extra_env = {**w.extra_env, "FAKE_LAB_GEN_CAPTURE": cap.name}
        w.run_once()
        with open(cap.name, encoding="utf-8") as fh:
            prompts = [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        results = self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner)).json()
        return prompts, {m["metric_name"]: m for m in results["metrics"]}, results

    def test_a_correct_answer_that_cites_only_an_invented_quote_is_judged_on_its_answer_but_earns_no_source_credit(self):
        project, owner = self.frozen()
        content, key, _ = self.judged_file(owner, project, lambda page, text: [{"page": page, "text": self.FAKE}])
        run = self.upload(owner, project, content).json()["run_id"]
        prompts, metrics, results = self.judge_with_capture(owner, project, run)
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("ZZQ", prompts[0]["user"])                      # the judge never saw the invented quote
        self.assertNotIn("cheese", prompts[0]["user"])
        self.assertIn('cites=""', prompts[0]["user"])                    # the claim has no passage to be supported by
        self.assertIn("NO PASSAGES WERE OFFERED", prompts[0]["user"])    # and the judge is told so: the facts are judged against the answer alone (#197)
        self.assertIn("judge-v3.1", json.dumps(results["judge"]))
        self.assertAlmostEqual(metrics["correctness"]["mean"], 100.0 / 18, places=6)     # the ANSWER is still judged: 1 of 18 at 100, the 17 abstentions score 0
        with self.connect() as c:
            row = c.execute("SELECT raw_value FROM question_scores s JOIN questions q ON q.id = s.question_id WHERE s.evaluation_id = %s "
                            "AND q.question_key = %s AND s.metric_name = 'correctness'", (results["evaluation"]["id"], key)).fetchone()
        self.assertEqual(row[0], 100.0)
        for name in ("faithfulness", "citation_precision", "citation_completeness"):
            self.assertEqual((metrics[name]["scored"], metrics[name]["mean"]), (1, 0.0), name)       # nothing real supports it: 0, whatever the model said
        sc = results["source_checks"]
        self.assertEqual((sc["total"], sc["found"], sc["not_found"], sc["excluded_from_judge"], sc["page_only_retrieval_credit"]), (1, 0, 1, 1, 1))
        self.assertIn("excluded from the judge", sc["note"])
        self.assertEqual(results["judge"]["judged_questions"], 1)

    def test_a_mixed_answer_keeps_only_the_found_source_the_system_text_not_the_developers_and_the_invented_citation_counts_against_precision(self):
        project, owner = self.frozen()
        content, key, text = self.judged_file(owner, project, lambda page, t: [
            {"page": page, "text": t.upper().replace(" ", "  ")},          # found (case and spacing differ)
            {"page": page, "text": self.FAKE}])                             # not found
        run = self.upload(owner, project, content).json()["run_id"]
        prompts, metrics, results = self.judge_with_capture(owner, project, run)
        user = prompts[0]["user"]
        self.assertNotIn("ZZQ", user)
        self.assertIn(text, user)                                           # the system parse's own text of the matched unit
        self.assertNotIn(text.upper().replace(" ", "  "), user)             # not the developer's version of it
        self.assertIn('cites="s1"', user)                                   # only the found source is offered to the judge
        self.assertEqual(metrics["faithfulness"]["mean"], 100.0)            # the claim IS supported by the real passage
        self.assertEqual(metrics["citation_precision"]["mean"], 50.0)       # 1 of its 2 citations supports it; the invented one counts against
        self.assertEqual(metrics["citation_completeness"]["mean"], 100.0)
        sc = results["source_checks"]
        self.assertEqual((sc["total"], sc["found"], sc["not_found"]), (2, 1, 1))
        listed = self.get(owner, project, f"{BASE}/submissions").json()["items"][0]
        self.assertEqual(listed["source_checks"]["not_found"], 1)

    def test_when_every_source_is_found_nothing_is_excluded_and_the_judge_sees_the_system_text(self):
        project, owner = self.frozen()
        content, key, text = self.judged_file(owner, project, lambda page, t: [{"page": page, "text": t}])
        run = self.upload(owner, project, content).json()["run_id"]
        prompts, metrics, results = self.judge_with_capture(owner, project, run)
        self.assertIn(text, prompts[0]["user"])
        self.assertEqual([metrics[n]["mean"] for n in ("faithfulness", "citation_precision", "citation_completeness")], [100.0, 100.0, 100.0])
        self.assertEqual(results["source_checks"]["excluded_from_judge"], 0)

    def test_an_answer_without_any_source_is_unsupported_and_a_builtin_run_has_no_source_checks(self):
        project, owner = self.frozen()
        content, key, _ = self.judged_file(owner, project, lambda page, text: [])
        run = self.upload(owner, project, content).json()["run_id"]
        prompts, metrics, results = self.judge_with_capture(owner, project, run)
        self.assertEqual(metrics["faithfulness"]["mean"], 0.0)
        self.assertEqual(metrics["citation_precision"]["mean"], 0.0)       # answerable, no citations: 0
        self.assertIsNone(results["source_checks"])                        # no sources were submitted at all
        baseline = self.api("post", owner, f"{BASE}/baseline-run", project, {}).json()["run_id"]
        self.assertIsNone(self.client.get(f"/api/v1/runs/{baseline}/results", headers=self.headers(owner)).json()["source_checks"])

    def test_a_file_with_problems_imports_nothing_until_the_owner_accepts_the_valid_lines(self):
        project, owner = self.frozen()
        good = self.file(owner, project, skip={4})
        keys = [json.loads(x)["question_id"] for x in good.splitlines()[1:]]
        skipped = sorted(self.gold(owner, project))[4]                      # the question the good file leaves out
        bad = good + "\n".join(["{broken", json.dumps({"question_id": "q-unknown", "status": "answered", "answer": "x", "sources": []}),
                                json.dumps({"question_id": keys[0], "status": "answered", "answer": "dup", "sources": []}),
                                json.dumps({"question_id": skipped, "status": "answered", "answer": "far", "sources": [{"page": 9, "text": "page nine text"}]})]) + "\n"
        res = self.upload(owner, project, bad)
        self.assertEqual(res.status_code, 422, res.text)
        report = res.json()["error"]["details"]["report"]
        codes = [(p["line"], p["code"]) for p in report["problems"]]
        self.assertEqual([c for _, c in codes], ["invalid_json", "unknown_question", "duplicate_question", "page_out_of_range"])
        self.assertEqual(report["problem_count"], 4)
        self.assertNotIn("q-unknown", res.text)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM external_submissions WHERE project_id = %s", (project,)).fetchone()[0], 0)       # nothing was imported
        self.assertEqual(self.get(owner, project, f"{BASE}/submissions").json()["items"], [])
        ok = self.upload(owner, project, bad, accept_partial=True)
        self.assertEqual(ok.status_code, 201, ok.text)
        rep = ok.json()["report"]
        self.assertEqual((rep["answered"], rep["not_submitted"], rep["problem_count"]), (17, 1, 4))      # the skipped question's only line was invalid: not submitted
        listed = self.get(owner, project, f"{BASE}/submissions").json()["items"]
        self.assertEqual((len(listed), listed[0]["imported_only_valid_lines"]), (1, True))

    def test_nothing_valid_in_the_file_is_refused_and_a_not_frozen_benchmark_takes_no_upload(self):
        project, owner = self.frozen()
        res = self.upload(owner, project, "{broken\n", accept_partial=True)
        self.assertEqual(res.status_code, 422)
        self.assertIn("no_valid_line", res.text)
        for body in ({"method_name": "m"}, {"method_name": "m", "file_content": 5}, {"method_name": "m", "file_content": "", "extra": 1},
                     {"method_name": "m", "file_content": "", "accept_partial": "yes"}):
            self.assertEqual(self.client.post(f"{BASE}/submissions?project_id={project}", headers=self.headers(owner), json=body).status_code, 400, body)
        blank = self.client.post(f"{BASE}/submissions?project_id={project}", headers=self.headers(owner), json={"method_name": "  ", "file_content": "x"})
        self.assertEqual(blank.status_code, 409)                                            # a method needs a name
        self.assertIn("method_name", blank.text)
        planned_project, planned_owner = self.planned()
        res = self.upload(planned_owner, planned_project, "x")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("frozen", res.text)

    def test_roles_a_reupload_is_a_new_run_and_two_uploads_are_listed_side_by_side(self):
        project, owner = self.frozen()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            participant = self.add_member(c, project, "participant")
            reviewer = self.add_member(c, project, "reviewer")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        content = self.file(owner, project)
        for who, code in ((participant, 403), (reviewer, 403), (outsider, 404)):
            self.assertEqual(self.upload(who, project, content).status_code, code, who)
        first = self.upload(maintainer, project, content)
        self.assertEqual(first.status_code, 201, first.text)
        second = self.upload(owner, project, self.file(owner, project, abstain={0, 1, 2}), method_name="file-method")
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["run_id"], second.json()["run_id"])                    # a re-upload never overwrites
        self.assertEqual((first.json()["method_revision"], second.json()["method_revision"]), (1, 2))
        third = self.upload(owner, project, content, method_name="other method")
        listed = self.get(owner, project, f"{BASE}/submissions").json()["items"]
        self.assertEqual([i["method_name"] for i in listed], ["other method", "file-method", "file-method"])
        self.assertEqual(listed[2]["counts"]["answered"], 18)
        self.assertEqual(listed[1]["counts"]["abstained"], 3)
        self.assertFalse(any(i["judged"] for i in listed))
        self.assertEqual(self.get(participant, project, f"/api/v1/runs/{first.json()['run_id']}/submission-details").status_code, 403)
        self.assertEqual(self.get(reviewer, project, f"/api/v1/runs/{first.json()['run_id']}/submission-details").status_code, 403)
        # the existing comparison endpoint answers for two uploads (it names what is not comparable yet: answer metrics are pending)
        cmp = self.client.get(f"/api/v1/comparisons?run_ids={first.json()['run_id']},{third.json()['run_id']}", headers=self.headers(owner, **{"X-Project-Id": str(project)}))
        self.assertEqual(cmp.status_code, 200, cmp.text)
        for run in (first, second, third):
            res = self.client.get(f"/api/v1/runs/{run.json()['run_id']}/results", headers=self.headers(owner)).json()
            self.assertIn("recall_at_k", {m["metric_name"] for m in res["metrics"]})


    # --- Issue #197: gold evidence is the units that support each fact; a located quote beats an invented one; a changed number is not "stated" ---

    def score_of(self, evaluation, key, metric):
        with self.connect() as c:
            return c.execute("SELECT raw_value, reason_codes FROM question_scores s JOIN questions q ON q.id = s.question_id WHERE s.evaluation_id = %s "
                             "AND q.question_key = %s AND s.metric_name = %s", (evaluation, key, metric)).fetchone()

    def test_new_questions_have_fact_level_gold_and_the_results_say_so(self):
        project, owner = self.frozen()
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT DISTINCT q.payload->>'gold_granularity' FROM questions q JOIN benchmark_revisions b ON b.id = q.benchmark_id "
                                       "WHERE b.project_id = %s", (project,)).fetchall(), [("fact",)])
        run = self.upload(owner, project, self.file(owner, project)).json()["run_id"]
        results = self.client.get(f"/api/v1/runs/{run}/results", headers=self.headers(owner)).json()
        self.assertEqual(results["retrieval_gold"]["fact_level_questions"], 18)
        self.assertEqual(results["retrieval_gold"]["page_level_questions"], 0)
        self.assertIn("fact-level gold", results["retrieval_gold"]["note"])

    def test_an_invented_quote_earns_no_retrieval_credit_with_fact_level_gold(self):
        project, owner = self.frozen()
        content = self.file(owner, project, fabricate={0})
        key = sorted(self.gold(owner, project))[0]
        fact = self.upload(owner, project, content).json()
        self.assertEqual(self.score_of(fact["evaluation_id"], key, "recall_at_k")[0], 0.0)       # was 100: the whole-page fallback covered every gold unit
        self.assertEqual(self.score_of(fact["evaluation_id"], key, "complete_at_k")[0], 0.0)

    def test_a_benchmark_with_page_level_gold_keeps_the_page_credit_and_the_results_say_so(self):
        project, owner = self.frozen(legacy_gold=True)
        key = sorted(self.gold(owner, project))[0]
        legacy = self.upload(owner, project, self.file(owner, project, fabricate={0})).json()
        self.assertEqual(self.score_of(legacy["evaluation_id"], key, "recall_at_k")[0], 100.0)
        results = self.client.get(f"/api/v1/runs/{legacy['run_id']}/results", headers=self.headers(owner)).json()
        self.assertEqual(results["retrieval_gold"]["page_level_questions"], 18)
        self.assertIn("PAGE-level gold", results["retrieval_gold"]["note"])
        self.assertIn("new benchmark revision", results["retrieval_gold"]["note"])

    def test_a_located_quote_beats_an_invented_one_on_retrieval(self):
        project, owner = self.frozen()
        run = self.upload(owner, project, self.file(owner, project, fabricate={0})).json()
        key0, key1 = sorted(self.gold(owner, project))[:2]
        self.assertGreater(self.score_of(run["evaluation_id"], key1, "recall_at_k")[0], self.score_of(run["evaluation_id"], key0, "recall_at_k")[0])

    def test_a_changed_number_is_not_stated_even_when_the_judge_says_it_is(self):
        project, owner = self.frozen()
        gold = self.gold(owner, project)
        key = sorted(gold)[0]
        page, text, answer = gold[key]
        wrong = answer.replace("1", "7").replace("2", "8").replace("3", "9")
        self.assertNotEqual(wrong, answer, "the fixture's fact has a number to change")
        lines = [json.dumps({"question_id": key, "status": "answered", "answer": wrong, "sources": [{"page": page, "text": text}]})]
        lines += [json.dumps({"question_id": k, "status": "abstained", "reason": "skip"}) for k in sorted(gold)[1:]]
        run = self.upload(owner, project, "\n".join(lines) + "\n").json()["run_id"]
        prompts, metrics, results = self.judge_with_capture(owner, project, run, mode="judge_ok")        # the fake judge says every fact is "stated"
        value, codes = self.score_of(results["evaluation"]["id"], key, "correctness")
        self.assertEqual(value, 0.0)
        self.assertIn("judge_overruled_by_number_check", codes)
        self.assertEqual(results["judge"]["questions_overruled_by_number_check"], 1)

    def test_no_span_no_credit_a_stated_fact_without_a_span_from_the_answer_scores_zero(self):
        project, owner = self.frozen()
        gold = self.gold(owner, project)
        key = sorted(gold)[0]
        page, text, answer = gold[key]
        for mode, what in (("judge_no_span", "no span at all"), ("judge_fake_span", "a span that is not in the answer")):
            with self.subTest(what=what):
                lines = [json.dumps({"question_id": key, "status": "answered", "answer": answer, "sources": [{"page": page, "text": text}]})]
                lines += [json.dumps({"question_id": k, "status": "abstained", "reason": "skip"}) for k in sorted(gold)[1:]]
                run = self.upload(owner, project, "\n".join(lines) + "\n").json()["run_id"]
                prompts, metrics, results = self.judge_with_capture(owner, project, run, mode=mode)
                value, codes = self.score_of(results["evaluation"]["id"], key, "correctness")
                self.assertEqual(value, 0.0)
                self.assertIn("judge_span_not_in_answer", codes)
                self.assertEqual(results["judge"]["questions_with_a_span_not_in_the_answer"], 1)
                self.assertIn("answer_span", prompts[0]["system"])


if __name__ == "__main__":
    unittest.main()
