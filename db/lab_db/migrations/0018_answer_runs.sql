-- 0018: method runs of the answering method as jobs (PM-DECISION on Issue #7, item (a), Refs #12).
--
-- generate_jobs gets a third phase, 'answer': a job that runs the participant-side answering method on a FROZEN benchmark and
-- records its answers through the existing run/attempt/submission tables. The reserve-before-spawn ledger (llm_calls), the
-- per-attempt rows, the lease/heartbeat/cancel machinery and the 'budget cap = this run's budget' rule are reused unchanged.
--   * run_id is set if and only if phase = 'answer' (a CHECK), and points at the run the job fills;
--   * at most ONE active answer job per run is guaranteed by the run being created together with its job; two concurrent
--     method runs on one benchmark are refused by the API (409) rather than scoped per run.
-- submissions gets failure_reason: a fixed code for a question the METHOD did not (validly) answer because of budget, provider,
-- contract or unknown-outcome problems. It is distinct from a model's own abstention (answer_status = 'abstained' with the
-- model's reason): only an abstained submission can carry it, and results report it separately.

ALTER TABLE generate_jobs DROP CONSTRAINT generate_jobs_phase_check;
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_phase_check CHECK (phase IN ('generate', 'validate', 'answer'));
ALTER TABLE generate_jobs ADD COLUMN run_id uuid;
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_run_iff_answer CHECK ((phase = 'answer') = (run_id IS NOT NULL));
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_run_fk FOREIGN KEY (project_id, run_id)
    REFERENCES runs(project_id, id) ON DELETE RESTRICT;
CREATE INDEX generate_jobs_run_idx ON generate_jobs (project_id, run_id) WHERE run_id IS NOT NULL;

ALTER TABLE submissions ADD COLUMN failure_reason text
    CHECK (failure_reason IS NULL OR failure_reason IN ('budget_exhausted', 'provider_error', 'invalid_model_reply', 'outcome_unknown'));
ALTER TABLE submissions ADD CONSTRAINT submissions_failure_is_an_abstention
    CHECK (failure_reason IS NULL OR answer_status = 'abstained');
