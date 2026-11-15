-- 0016: automated validation of generated questions (P-D slice 4, Refs #7, Refs #10).
--
-- generate_jobs gets a phase: 'generate' (writes questions) or 'validate' (checks them). question_validations is the
-- append-only record of one validation run per question: free code checks (grounding of the reference answer and facts in
-- the text that was shown), the validator call's reported flags, and the verdict. Nothing is deleted or repaired by a
-- verdict. The validator is the SAME provider and model as the generator (a separate call with a separate prompt), so
-- this is not independent in the strong sense; "needs outside context" is the validator's self-report, not a proof.
-- Same gold RLS and frozen guard as gold_answers (0015): the rows carry reasoning about the gold.

ALTER TABLE generate_jobs ADD COLUMN phase text NOT NULL DEFAULT 'generate' CHECK (phase IN ('generate', 'validate'));

CREATE TABLE question_validations (
    id                 uuid PRIMARY KEY,
    benchmark_id       uuid NOT NULL,
    question_id        uuid NOT NULL,
    job_id             uuid NOT NULL,
    validator_revision text NOT NULL CHECK (length(validator_revision) BETWEEN 1 AND 128),
    verdict            text NOT NULL CHECK (verdict IN ('pass', 'fail', 'needs_review')),
    code_checks        jsonb NOT NULL CHECK (jsonb_typeof(code_checks) = 'object' AND octet_length(code_checks::text) <= 65536),
    llm_checks         jsonb CHECK (llm_checks IS NULL OR (jsonb_typeof(llm_checks) = 'object' AND octet_length(llm_checks::text) <= 65536)),
    reason             text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question_id, job_id),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES generate_jobs(job_id) ON DELETE RESTRICT
);
CREATE INDEX question_validations_benchmark_idx ON question_validations (benchmark_id);
CREATE TRIGGER question_validations_frozen BEFORE INSERT OR UPDATE OR DELETE ON question_validations
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER question_validations_append_only BEFORE UPDATE OR DELETE ON question_validations
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

ALTER TABLE question_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE question_validations FORCE ROW LEVEL SECURITY;
CREATE POLICY question_validations_select_gold ON question_validations
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = question_validations.benchmark_id)));
CREATE POLICY question_validations_insert_gold ON question_validations
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = question_validations.benchmark_id)));
