-- 0003: methods, runs, attempts, submissions, hits/citations, evaluations
-- and scores (SPEC 7, 9, 10.1, 16.3, 16.5). Scores can only point at the
-- run's own submissions and at questions of the run's benchmark revision.

CREATE TABLE method_revisions (
    id              uuid PRIMARY KEY,
    project_id      uuid NOT NULL,
    resource_id     uuid NOT NULL UNIQUE,
    owner_actor_id  uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    protocol_version text NOT NULL CHECK (protocol_version ~ '^[0-9]+\.[0-9]+$'),
    config_hash     text NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    execution_mode  text NOT NULL CHECK (execution_mode IN ('http', 'jsonl')),
    visibility      text NOT NULL CHECK (visibility IN ('lab_observed', 'declared_only')),
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, resource_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT
);

CREATE TYPE run_status AS ENUM ('queued', 'preparing', 'running', 'awaiting_submissions', 'evaluating',
    'completed', 'completed_with_errors', 'review_required', 'paused_budget', 'failed', 'cancelled');

CREATE TABLE runs (
    id                  uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    benchmark_id        uuid NOT NULL,
    method_revision_id  uuid NOT NULL,
    status              run_status NOT NULL DEFAULT 'queued',
    row_version         integer NOT NULL DEFAULT 1,
    budget_profile      text NOT NULL CHECK (length(budget_profile) BETWEEN 1 AND 128),
    finalized_at        timestamptz,
    UNIQUE (project_id, id),
    UNIQUE (id, benchmark_id),
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, method_revision_id) REFERENCES method_revisions(project_id, id) ON DELETE RESTRICT
);

-- Runs evaluate frozen benchmarks only.
CREATE FUNCTION runs_require_frozen_benchmark() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT status FROM benchmark_revisions WHERE id = NEW.benchmark_id) <> 'frozen' THEN
        RAISE EXCEPTION 'run % needs a frozen benchmark revision', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER runs_require_frozen_benchmark BEFORE INSERT ON runs
    FOR EACH ROW EXECUTE FUNCTION runs_require_frozen_benchmark();

CREATE TABLE attempts (
    id              uuid PRIMARY KEY,
    run_id          uuid NOT NULL,
    benchmark_id    uuid NOT NULL,
    question_id     uuid NOT NULL,
    attempt_number  integer NOT NULL CHECK (attempt_number >= 1),
    transport       text NOT NULL CHECK (transport IN ('http', 'jsonl')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, question_id, attempt_number),
    UNIQUE (run_id, id),
    FOREIGN KEY (run_id, benchmark_id) REFERENCES runs(id, benchmark_id) ON DELETE RESTRICT,
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);

CREATE TABLE submissions (
    id              uuid PRIMARY KEY,
    run_id          uuid NOT NULL,
    attempt_id      uuid NOT NULL,
    question_id     uuid NOT NULL,
    status          text NOT NULL CHECK (status IN ('accepted', 'rejected', 'late')),
    answer_status   text CHECK (answer_status IN ('answered', 'abstained')),
    payload_hash    text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    c14n_version    text NOT NULL CHECK (length(c14n_version) BETWEEN 1 AND 32),
    payload_artifact_id uuid NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    received_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, id),
    FOREIGN KEY (run_id, attempt_id) REFERENCES attempts(run_id, id) ON DELETE RESTRICT,
    CHECK ((status = 'accepted') = (answer_status IS NOT NULL) OR status = 'late')
);
-- One accepted submission per attempt, and the first accepted terminal
-- answer per (run, question) is the one that counts (SPEC 16.5).
CREATE UNIQUE INDEX submissions_one_accepted_per_attempt ON submissions (attempt_id) WHERE status = 'accepted';
CREATE UNIQUE INDEX submissions_one_accepted_per_question ON submissions (run_id, question_id) WHERE status = 'accepted';

-- question_id must be the attempt's question.
CREATE FUNCTION submissions_match_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.question_id IS DISTINCT FROM (SELECT question_id FROM attempts WHERE id = NEW.attempt_id) THEN
        RAISE EXCEPTION 'submission question does not match its attempt' USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER submissions_match_attempt BEFORE INSERT ON submissions
    FOR EACH ROW EXECUTE FUNCTION submissions_match_attempt();
-- Received submissions are immutable (corrections need a new attempt/run).
CREATE TRIGGER submissions_immutable BEFORE UPDATE OR DELETE ON submissions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE retrieved_hits (
    submission_id   uuid NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
    hit_key         text NOT NULL CHECK (length(hit_key) BETWEEN 1 AND 128),
    rank            integer NOT NULL CHECK (rank >= 1),
    content         text NOT NULL,
    PRIMARY KEY (submission_id, hit_key),
    UNIQUE (submission_id, rank)
);
CREATE TRIGGER retrieved_hits_immutable BEFORE UPDATE OR DELETE ON retrieved_hits
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE citations (
    submission_id   uuid NOT NULL,
    claim_key       text NOT NULL CHECK (length(claim_key) BETWEEN 1 AND 128),
    hit_key         text NOT NULL,
    region_indices  integer[] NOT NULL CHECK (cardinality(region_indices) >= 1),
    FOREIGN KEY (submission_id, hit_key) REFERENCES retrieved_hits(submission_id, hit_key) ON DELETE RESTRICT
);
CREATE TRIGGER citations_immutable BEFORE UPDATE OR DELETE ON citations
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE evaluations (
    id              uuid PRIMARY KEY,
    run_id          uuid NOT NULL,
    benchmark_id    uuid NOT NULL,
    grader_revision text NOT NULL CHECK (length(grader_revision) BETWEEN 1 AND 128),
    status          text NOT NULL CHECK (status IN ('provisional', 'published')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, run_id, benchmark_id),
    FOREIGN KEY (run_id, benchmark_id) REFERENCES runs(id, benchmark_id) ON DELETE RESTRICT
);

CREATE TABLE question_scores (
    evaluation_id   uuid NOT NULL,
    run_id          uuid NOT NULL,
    benchmark_id    uuid NOT NULL,
    question_id     uuid NOT NULL,
    submission_id   uuid,              -- null: the method produced no valid answer
    metric_name     text NOT NULL CHECK (metric_name IN ('recall_at_k', 'complete_at_k', 'correctness',
                        'textual_consistency', 'localization_precision', 'citation_precision',
                        'citation_completeness', 'faithfulness')),
    status          text NOT NULL CHECK (status IN ('scored', 'not_applicable', 'pending', 'error')),
    raw_value       double precision CHECK (raw_value BETWEEN 0 AND 100),
    numerator       double precision CHECK (numerator >= 0),
    denominator     double precision CHECK (denominator >= 0),
    reason_codes    text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (evaluation_id, question_id, metric_name),
    FOREIGN KEY (evaluation_id, run_id, benchmark_id) REFERENCES evaluations(id, run_id, benchmark_id) ON DELETE RESTRICT,
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, submission_id) REFERENCES submissions(run_id, id) ON DELETE RESTRICT,
    CHECK (status <> 'scored' OR (raw_value IS NOT NULL AND numerator IS NOT NULL AND denominator > 0
           AND abs(raw_value - 100 * numerator / denominator) <= 1e-9 * greatest(1, abs(raw_value)))),
    CHECK (status = 'scored' OR raw_value IS NULL),
    CHECK (status NOT IN ('pending', 'error') OR (numerator IS NULL AND denominator IS NULL)),
    CHECK (status = 'scored' OR cardinality(reason_codes) >= 1)
);

CREATE TABLE fact_scores (
    evaluation_id   uuid NOT NULL,
    run_id          uuid NOT NULL,
    benchmark_id    uuid NOT NULL,
    question_id     uuid NOT NULL,
    fact_id         uuid NOT NULL,
    score           double precision CHECK (score IN (0, 0.5, 1)),
    status          text NOT NULL CHECK (status IN ('scored', 'pending', 'error')),
    reason          text NOT NULL CHECK (length(reason) >= 1),
    PRIMARY KEY (evaluation_id, question_id, fact_id),
    FOREIGN KEY (evaluation_id, run_id, benchmark_id) REFERENCES evaluations(id, run_id, benchmark_id) ON DELETE RESTRICT,
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (question_id, fact_id) REFERENCES facts(question_id, id) ON DELETE RESTRICT,
    CHECK ((status = 'scored') = (score IS NOT NULL))
);

-- 0.5 only where the fact defines a partial-credit rule (SPEC 16.2, T38).
CREATE FUNCTION fact_scores_partial_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.score = 0.5 AND (SELECT partial_credit_rule FROM facts WHERE id = NEW.fact_id) IS NULL THEN
        RAISE EXCEPTION 'fact % has no partial-credit rule', NEW.fact_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fact_scores_partial_rule BEFORE INSERT OR UPDATE ON fact_scores
    FOR EACH ROW EXECUTE FUNCTION fact_scores_partial_rule();

-- Published evaluations are immutable; re-grading creates a new evaluation.
CREATE FUNCTION scores_of_published_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE e uuid;
BEGIN
    e := CASE WHEN TG_OP = 'DELETE' THEN OLD.evaluation_id ELSE NEW.evaluation_id END;
    IF (SELECT status FROM evaluations WHERE id = e) = 'published' THEN
        RAISE EXCEPTION 'evaluation % is published', e USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER question_scores_published BEFORE INSERT OR UPDATE OR DELETE ON question_scores
    FOR EACH ROW EXECUTE FUNCTION scores_of_published_guard();
CREATE TRIGGER fact_scores_published BEFORE INSERT OR UPDATE OR DELETE ON fact_scores
    FOR EACH ROW EXECUTE FUNCTION scores_of_published_guard();

CREATE FUNCTION evaluations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status = 'published' THEN
        RAISE EXCEPTION 'evaluation % cannot change', OLD.id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.run_id, NEW.benchmark_id, NEW.grader_revision) IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.benchmark_id, OLD.grader_revision) THEN
        RAISE EXCEPTION 'evaluation identity is immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'published' AND EXISTS (
        SELECT 1 FROM question_scores WHERE evaluation_id = NEW.id AND status = 'pending') THEN
        RAISE EXCEPTION 'pending scores block publishing (SPEC 16.5)' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER evaluations_guard BEFORE UPDATE OR DELETE ON evaluations
    FOR EACH ROW EXECUTE FUNCTION evaluations_guard();

-- A score's submission must be the accepted answer to that same question.
CREATE FUNCTION question_scores_submission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.submission_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM submissions
        WHERE id = NEW.submission_id AND question_id = NEW.question_id AND status = 'accepted') THEN
        RAISE EXCEPTION 'score must reference the accepted submission of its question'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER question_scores_submission BEFORE INSERT OR UPDATE ON question_scores
    FOR EACH ROW EXECUTE FUNCTION question_scores_submission();
