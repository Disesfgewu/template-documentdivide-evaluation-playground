-- 0004: jobs with leases and fencing, graph checkpoints, node operations and
-- structured log events (SPEC 10.3, 12.2-12.3, 16.6).

CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'failed', 'paused_budget',
                                'waiting_review', 'cancel_requested', 'cancelled');

CREATE TABLE job_transitions_allowed (
    from_status job_status NOT NULL,
    to_status   job_status NOT NULL,
    PRIMARY KEY (from_status, to_status)
);
-- SPEC 16.6 table, verbatim.
INSERT INTO job_transitions_allowed VALUES
    ('queued', 'running'), ('queued', 'cancelled'),
    ('running', 'succeeded'), ('running', 'failed'), ('running', 'paused_budget'),
    ('running', 'waiting_review'), ('running', 'cancel_requested'), ('running', 'queued'),
    ('paused_budget', 'queued'), ('paused_budget', 'cancelled'),
    ('waiting_review', 'queued'), ('waiting_review', 'cancelled'),
    ('cancel_requested', 'cancelled'),
    ('failed', 'queued');

CREATE SEQUENCE lease_tokens;  -- fencing tokens are globally monotonic

CREATE TABLE jobs (
    id               uuid PRIMARY KEY,
    project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind             text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    stage            text,                          -- business stage, separate from status
    status           job_status NOT NULL DEFAULT 'queued',
    row_version      integer NOT NULL DEFAULT 1,
    attempt          integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts     integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    retryable        boolean NOT NULL DEFAULT false,
    lease_owner      text,
    lease_token      bigint,
    lease_expires_at timestamptz,
    UNIQUE (project_id, id),
    CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE TABLE job_transitions (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       uuid NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    from_status  job_status NOT NULL,
    to_status    job_status NOT NULL,
    lease_token  bigint,
    reason       text NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER job_transitions_append_only BEFORE UPDATE OR DELETE ON job_transitions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE FUNCTION jobs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'jobs are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'cancelled') THEN
        RAISE EXCEPTION 'job % is terminal (%); create a new job', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT EXISTS (
        SELECT 1 FROM job_transitions_allowed WHERE from_status = OLD.status AND to_status = NEW.status) THEN
        RAISE EXCEPTION 'illegal job transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'failed' AND NEW.status = 'queued' AND NOT (OLD.retryable AND OLD.attempt < OLD.max_attempts) THEN
        RAISE EXCEPTION 'job % is not retryable or has no attempts left', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.lease_token IS NOT NULL AND OLD.lease_token IS NOT NULL AND NEW.lease_token < OLD.lease_token THEN
        RAISE EXCEPTION 'fencing token may not go backwards' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO job_transitions (job_id, from_status, to_status, lease_token, reason)
        VALUES (NEW.id, OLD.status, NEW.status, COALESCE(NEW.lease_token, OLD.lease_token),
                COALESCE(current_setting('lab.transition_reason', true), 'unspecified'));
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER jobs_guard BEFORE UPDATE OR DELETE ON jobs FOR EACH ROW EXECUTE FUNCTION jobs_guard();

CREATE TABLE graph_threads (
    thread_id            text PRIMARY KEY CHECK (length(thread_id) BETWEEN 1 AND 128),
    project_id           uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    job_id               uuid NOT NULL,
    workflow_revision    text NOT NULL CHECK (length(workflow_revision) BETWEEN 1 AND 128),
    continues_thread_id  text REFERENCES graph_threads(thread_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE RESTRICT
);

CREATE TABLE graph_checkpoints (
    thread_id             text NOT NULL REFERENCES graph_threads(thread_id) ON DELETE RESTRICT,
    seq                   integer NOT NULL CHECK (seq >= 1),
    lease_token           bigint NOT NULL,
    workflow_revision     text NOT NULL,
    state_schema_version  integer NOT NULL CHECK (state_schema_version >= 1),
    state                 jsonb NOT NULL,
    steps                 integer NOT NULL CHECK (steps >= 0),
    last_node             text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, seq)
);
CREATE TRIGGER graph_checkpoints_append_only BEFORE UPDATE OR DELETE ON graph_checkpoints
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE node_operations (
    id                     uuid PRIMARY KEY,
    job_id                 uuid NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    work_item_id           text NOT NULL CHECK (length(work_item_id) BETWEEN 1 AND 256),
    node_revision          text NOT NULL CHECK (length(node_revision) BETWEEN 1 AND 128),
    logical_invocation_id  uuid NOT NULL,
    attempt                integer NOT NULL CHECK (attempt >= 1),
    status                 text NOT NULL CHECK (status IN ('intent', 'succeeded', 'failed', 'outcome_unknown')),
    result_artifact_id     uuid REFERENCES artifacts(id) ON DELETE RESTRICT,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (work_item_id, node_revision, logical_invocation_id, attempt),
    CHECK (status <> 'succeeded' OR result_artifact_id IS NOT NULL)
);
-- At most one succeeded attempt per logical invocation: retries reuse it.
CREATE UNIQUE INDEX node_operations_one_success
    ON node_operations (work_item_id, node_revision, logical_invocation_id) WHERE status = 'succeeded';

CREATE TABLE log_events (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            uuid NOT NULL UNIQUE,      -- re-sends de-duplicate on this
    schema_version      integer NOT NULL CHECK (schema_version >= 1),
    occurred_at         timestamptz NOT NULL,
    observed_at         timestamptz NOT NULL DEFAULT now(),
    level               text NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    event_name          text NOT NULL CHECK (event_name ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$'),
    message             text NOT NULL,
    trace_id            text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    span_id             text CHECK (span_id ~ '^[0-9a-f]{16}$'),
    parent_span_id      text CHECK (parent_span_id ~ '^[0-9a-f]{16}$'),
    service             text NOT NULL,
    component           text NOT NULL,
    operation_name      text NOT NULL,
    project_id          uuid REFERENCES projects(id) ON DELETE RESTRICT,
    operation_id        uuid,
    job_id              uuid,
    run_id              uuid,
    question_id         uuid,
    status              text,
    duration_ms         double precision CHECK (duration_ms >= 0),
    error_code          text,
    error_message       text,
    usage_known         boolean,
    telemetry_origin    text NOT NULL CHECK (telemetry_origin IN ('lab_observed', 'participant_reported')),
    redaction_policy    text NOT NULL,
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX log_events_trace_idx ON log_events (trace_id, occurred_at);
CREATE INDEX log_events_run_question_idx ON log_events (run_id, question_id, occurred_at);
CREATE TRIGGER log_events_append_only BEFORE UPDATE OR DELETE ON log_events
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
