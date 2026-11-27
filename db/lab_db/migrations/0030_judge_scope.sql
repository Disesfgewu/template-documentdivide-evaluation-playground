-- 0030: a judge job can be limited to the questions whose judged scores came back as an ERROR the judge side caused (Issue #215 item 3, PM-DECISION).
-- 'all' is every judge job so far. 'errors' re-judges ONLY those questions: same model, same prompt revision as the evaluation it extends, the other
-- rows are copied unchanged into the new evaluation. The calls are in the ledger like any judge call (llm_calls, the job's budget).
ALTER TABLE generate_jobs ADD COLUMN judge_scope text NOT NULL DEFAULT 'all' CHECK (judge_scope IN ('all', 'errors'));
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_scope_only_for_judge CHECK (judge_scope = 'all' OR phase = 'judge');
