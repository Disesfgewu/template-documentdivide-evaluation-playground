-- 0019: the semantic judge as a job (PM-DECISION on Issue #7, judge design GO, Refs #12).
--
-- generate_jobs gets a fourth phase, 'judge': a job that judges the answered submissions of ONE finished method run (one
-- lab-gen call per (question, submission)) and writes a NEW evaluation for that run (re-judging never updates old score
-- rows). Like 'answer' it names its run (run_id is set iff phase is 'answer' or 'judge'). The ledger, lease, cancel and the
-- per-benchmark budget are reused unchanged.
-- question_scores gets the judge provenance: the model, the prompt revision, the sha256 of the fixed prompt text and the
-- limitation text. They are all set together or all null (null = the row was not produced from a judge reply), so a judged
-- score can never exist without its same-model / uncalibrated / provisional limitation.

ALTER TABLE generate_jobs DROP CONSTRAINT generate_jobs_phase_check;
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_phase_check CHECK (phase IN ('generate', 'validate', 'answer', 'judge'));
ALTER TABLE generate_jobs DROP CONSTRAINT generate_jobs_run_iff_answer;
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_run_iff_run_phase CHECK ((phase IN ('answer', 'judge')) = (run_id IS NOT NULL));

ALTER TABLE question_scores ADD COLUMN judge_model text CHECK (judge_model IS NULL OR length(judge_model) BETWEEN 1 AND 128);
ALTER TABLE question_scores ADD COLUMN judge_prompt_revision text CHECK (judge_prompt_revision IS NULL OR length(judge_prompt_revision) BETWEEN 1 AND 64);
ALTER TABLE question_scores ADD COLUMN judge_prompt_sha256 text CHECK (judge_prompt_sha256 IS NULL OR judge_prompt_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE question_scores ADD COLUMN judge_limitation text CHECK (judge_limitation IS NULL OR length(judge_limitation) BETWEEN 1 AND 1000);
ALTER TABLE question_scores ADD CONSTRAINT question_scores_judge_provenance_together CHECK (
    (judge_model IS NULL) = (judge_prompt_revision IS NULL) AND (judge_model IS NULL) = (judge_prompt_sha256 IS NULL)
    AND (judge_model IS NULL) = (judge_limitation IS NULL));
