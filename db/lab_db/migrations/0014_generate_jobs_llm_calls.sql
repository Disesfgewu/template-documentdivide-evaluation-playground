-- 0014: the input of a generation job and the ledger of every LLM call it made (P-D slice 2, Refs #7, Refs #10).
--
-- generate_jobs binds one 'document.generate' job to a benchmark revision, the model pin and the per-benchmark budget
-- cap. llm_calls is the append-only record of each call lab-gen made: what model, how many tokens, what it cost
-- (micro-currency from CONFIGURED prices), the outcome, and the sha256 + length of the request and the response.
-- It NEVER holds the API key, a prompt or the model's text (raw provider payloads stay out of the database).
--
-- Budget rule (fail-closed): spent = sum of known charges; held = worst-case reservations kept for calls whose cost is
-- unknown (usage not reported, response lost). A benchmark's remaining budget = cap - spent - held; unknown is never free.

CREATE TABLE generate_jobs (
    job_id              uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    benchmark_id        uuid NOT NULL,
    operation_id        uuid NOT NULL,
    requested_by        uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    model               text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
    max_output_tokens   integer NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 500),   -- owner hard cap (Issue #7, D7)
    budget_cap_micros   bigint NOT NULL CHECK (budget_cap_micros > 0),
    currency            text NOT NULL CHECK (length(currency) BETWEEN 1 AND 16),
    pricing_revision    text NOT NULL CHECK (length(pricing_revision) BETWEEN 1 AND 128),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, operation_id),
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX generate_jobs_benchmark_idx ON generate_jobs (project_id, benchmark_id, created_at);
CREATE TRIGGER generate_jobs_append_only BEFORE UPDATE OR DELETE ON generate_jobs
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE llm_calls (
    id               uuid PRIMARY KEY,
    project_id       uuid NOT NULL,
    benchmark_id     uuid NOT NULL,
    job_id           uuid NOT NULL,
    call_id          text NOT NULL CHECK (length(call_id) BETWEEN 1 AND 128),
    model            text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
    status           text NOT NULL CHECK (status IN ('ok', 'failed', 'skipped_budget', 'skipped_after_stop', 'outcome_unknown')),
    reason           text CHECK (reason IS NULL OR length(reason) <= 200),
    usage_known      boolean NOT NULL,
    input_tokens     integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens    integer NOT NULL CHECK (output_tokens BETWEEN 0 AND 500),
    charged_micros   bigint NOT NULL CHECK (charged_micros >= -1),     -- -1 = unknown, never recorded as zero
    held_micros      bigint NOT NULL CHECK (held_micros >= 0),
    attempts         integer NOT NULL CHECK (attempts >= 0),
    currency         text NOT NULL,
    pricing_revision text NOT NULL,
    request_sha256   text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_length  integer NOT NULL CHECK (response_length >= 0),
    response_sha256  text CHECK (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, call_id),
    FOREIGN KEY (project_id, job_id) REFERENCES generate_jobs(project_id, job_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    -- a known cost needs known usage; an unknown cost must hold its worst case; a call that never ran costs nothing
    CHECK ((usage_known AND charged_micros >= 0 AND held_micros = 0)
        OR (NOT usage_known AND charged_micros = -1 AND status IN ('ok', 'outcome_unknown') AND held_micros > 0)
        OR (NOT usage_known AND charged_micros = -1 AND status IN ('failed', 'skipped_budget', 'skipped_after_stop') AND held_micros = 0))
);
CREATE INDEX llm_calls_benchmark_idx ON llm_calls (project_id, benchmark_id);
CREATE TRIGGER llm_calls_append_only BEFORE UPDATE OR DELETE ON llm_calls
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
