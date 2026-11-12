-- 0014: the input of a generation job and the ledger of every LLM call it made (P-D slice 2, Refs #7, Refs #10).
--
-- generate_jobs binds one 'document.generate' job to a benchmark revision, the model pin and the per-benchmark budget
-- cap. llm_calls is the append-only record of each call lab-gen made: what model, how many tokens, what it cost
-- (micro-currency from CONFIGURED prices), the outcome, and the sha256 + length of the request and the response.
-- It NEVER holds the API key, a prompt or the model's text (raw provider payloads stay out of the database).
--
-- Budget rule (fail-closed, reserve BEFORE spawning): before lab-gen is started, one transaction (per-benchmark advisory
-- lock) appends a 'reserved' row with the worst-case hold for every call that fits the remaining budget (calls that do
-- not fit get a 'skipped_budget' row and are never sent). After the child returns, a settlement row is appended per
-- call (ok / failed / outcome_unknown / ...). The ledger is append-only: the CURRENT state of a call is its latest row
-- (highest seq) per (job, attempt, call). spent = sum of known charges, held = sum of holds of the current rows, and a
-- benchmark's remaining budget = cap - spent - held. A worker that dies after spawning leaves its 'reserved' holds
-- in place, so a retry (a new attempt) sees the reduced budget; unknown is never free.

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
    UNIQUE (project_id, job_id),
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
    attempt          integer NOT NULL CHECK (attempt >= 1),
    seq              bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    status           text NOT NULL CHECK (status IN ('reserved', 'ok', 'failed', 'skipped_budget', 'skipped_after_stop', 'outcome_unknown')),
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
    FOREIGN KEY (project_id, job_id) REFERENCES generate_jobs(project_id, job_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    -- a known cost needs known usage; an unknown cost must hold its worst case; a call that never ran costs nothing
    CHECK ((usage_known AND charged_micros >= 0 AND held_micros = 0)
        OR (NOT usage_known AND charged_micros = -1 AND status IN ('ok', 'outcome_unknown', 'reserved') AND held_micros > 0)
        OR (NOT usage_known AND charged_micros = -1 AND status IN ('failed', 'skipped_budget', 'skipped_after_stop') AND held_micros = 0))
);
CREATE INDEX llm_calls_benchmark_idx ON llm_calls (project_id, benchmark_id);
-- one reservation and one settlement per call of an attempt; a retry is a new attempt, so its rows never collide with
-- (or get silently dropped in favour of) an earlier attempt's rows
CREATE UNIQUE INDEX llm_calls_reservation_uq ON llm_calls (job_id, attempt, call_id) WHERE status = 'reserved';
CREATE UNIQUE INDEX llm_calls_settlement_uq ON llm_calls (job_id, attempt, call_id) WHERE status <> 'reserved';
CREATE TRIGGER llm_calls_append_only BEFORE UPDATE OR DELETE ON llm_calls
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
