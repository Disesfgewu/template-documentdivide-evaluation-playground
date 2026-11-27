-- 0028: what the generator rejected, kept owner-local for the LAST generation job of a benchmark (Issue #215, PM).
-- The replies are not stored; without the rejected facts and the units the model was shown nobody can say WHY a fact was not attributable or a
-- condition not grounded. This table holds them, in the database only (never in GitHub, never in app.log beyond the 120-character preview rule, no API
-- reads it): the worker deletes the rows of earlier jobs of the benchmark before it writes the rows of the job that just finished, and at most 400 rows
-- are kept per job. Read it as the database owner on the owner's stack.
CREATE TABLE generation_rejections (
    id           uuid PRIMARY KEY,
    project_id   uuid NOT NULL,
    benchmark_id uuid NOT NULL,
    job_id       uuid NOT NULL,
    call_id      text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('general', 'cross_page', 'confusable')),
    reason       text NOT NULL CHECK (length(reason) <= 200),
    question     text NOT NULL,
    facts        jsonb NOT NULL,
    shown        jsonb NOT NULL,      -- [[evidence_id, text up to 400 characters], ...] of the units the model was shown
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX generation_rejections_job ON generation_rejections (benchmark_id, job_id);
