-- 0007_integrity_hardening.sql
-- Hardens data integrity across 0001-0006 without modifying prior migrations:
-- 1. Append-only and identity/state immutability guards
-- 2. evidence_regions <-> evidence_units same-document check
-- 3. Committed, same-project artifact checks for submissions and node_operations
-- 4. Strengthened artifacts_guard (identity/retention immutability, legal_hold, timestamp rules)
-- 5. Evaluations insert/publish completeness and concurrency locking
-- 6. question_scores mandatory accepted-submission link and FOR SHARE locking
-- 7. Runs state machine and transition history
-- 8. Benchmark transition logging, freeze question check, resource_versions sync, and FOR SHARE child guard
-- 9. Strengthened jobs_guard (immutable fields, attempt bounds, monotonic max_lease_token, lease-takeover transitions)
-- 10. Job-scoped uniqueness on node_operations
-- 11. NaN / Infinity numeric guards

-- ============================================================================
-- 1. Append-only tables & state/identity immutability guards
-- ============================================================================

CREATE TRIGGER lineage_edges_append_only
    BEFORE UPDATE OR DELETE ON lineage_edges
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER idempotency_keys_append_only
    BEFORE UPDATE OR DELETE ON idempotency_keys
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER document_versions_append_only
    BEFORE UPDATE OR DELETE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER pages_append_only
    BEFORE UPDATE OR DELETE ON pages
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER evidence_units_append_only
    BEFORE UPDATE OR DELETE ON evidence_units
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER evidence_regions_append_only
    BEFORE UPDATE OR DELETE ON evidence_regions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER attempts_append_only
    BEFORE UPDATE OR DELETE ON attempts
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER method_revisions_append_only
    BEFORE UPDATE OR DELETE ON method_revisions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER derivation_inputs_append_only
    BEFORE UPDATE OR DELETE ON derivation_inputs
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TRIGGER outbox_consumptions_append_only
    BEFORE UPDATE OR DELETE ON outbox_consumptions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

-- operations: only allow intent -> running, or intent/running -> succeeded/failed/outcome_unknown
-- (setting finished_at); all other columns are immutable; never deleted.
CREATE OR REPLACE FUNCTION operations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'operations rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.kind, NEW.actor_id, NEW.trace_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.kind, OLD.actor_id, OLD.trace_id, OLD.created_at) THEN
        RAISE EXCEPTION 'operation identity columns are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'intent' AND NEW.status = 'running' THEN
        IF NEW.finished_at IS DISTINCT FROM OLD.finished_at THEN
            RAISE EXCEPTION 'finished_at cannot be set while operation % is running', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status IN ('intent', 'running')
       AND NEW.status IN ('succeeded', 'failed', 'outcome_unknown') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'illegal operation transition % -> % (id %)', OLD.status, NEW.status, OLD.id
        USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE TRIGGER operations_guard
    BEFORE UPDATE OR DELETE ON operations
    FOR EACH ROW EXECUTE FUNCTION operations_guard();

-- outbox_events: only delivered_at may change from NULL to non-NULL; never deleted.
CREATE OR REPLACE FUNCTION outbox_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'outbox_events rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.event_id, NEW.project_id, NEW.topic, NEW.payload, NEW.operation_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.event_id, OLD.project_id, OLD.topic, OLD.payload, OLD.operation_id, OLD.created_at) THEN
        RAISE EXCEPTION 'outbox_events identity and payload are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.delivered_at IS NOT NULL OR NEW.delivered_at IS NULL THEN
        RAISE EXCEPTION 'outbox_events delivered_at may only transition from NULL to non-NULL (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER outbox_events_guard
    BEFORE UPDATE OR DELETE ON outbox_events
    FOR EACH ROW EXECUTE FUNCTION outbox_events_guard();

-- node_operations: only allow intent/outcome_unknown -> succeeded/failed (and setting result_artifact_id);
-- identity columns immutable; never deleted; result_artifact_id (when non-null) must belong to job project and be committed.
CREATE OR REPLACE FUNCTION node_operations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_job_project_id uuid;
    v_art_project_id uuid;
    v_art_state      artifact_state;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'node_operations rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (NEW.id, NEW.job_id, NEW.work_item_id, NEW.node_revision,
            NEW.logical_invocation_id, NEW.attempt, NEW.created_at)
           IS DISTINCT FROM
           (OLD.id, OLD.job_id, OLD.work_item_id, OLD.node_revision,
            OLD.logical_invocation_id, OLD.attempt, OLD.created_at) THEN
            RAISE EXCEPTION 'node_operation identity columns are immutable (id %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF NOT (OLD.status IN ('intent', 'outcome_unknown')
                AND NEW.status IN ('succeeded', 'failed')) THEN
            RAISE EXCEPTION 'illegal node_operation transition % -> % (id %)', OLD.status, NEW.status, OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF OLD.result_artifact_id IS NOT NULL
           AND NEW.result_artifact_id IS DISTINCT FROM OLD.result_artifact_id THEN
            RAISE EXCEPTION 'node_operation result_artifact_id cannot be overwritten (id %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    IF NEW.result_artifact_id IS NOT NULL THEN
        SELECT project_id INTO v_job_project_id FROM jobs WHERE id = NEW.job_id;
        SELECT project_id, state INTO v_art_project_id, v_art_state
        FROM artifacts WHERE id = NEW.result_artifact_id;

        IF v_art_project_id IS DISTINCT FROM v_job_project_id
           OR v_art_state IS DISTINCT FROM 'committed'::artifact_state THEN
            RAISE EXCEPTION 'node_operation result_artifact_id % must belong to job project and be committed',
                NEW.result_artifact_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER node_operations_guard
    BEFORE INSERT OR UPDATE OR DELETE ON node_operations
    FOR EACH ROW EXECUTE FUNCTION node_operations_guard();

-- parse_runs: only allow status running -> terminal; identity columns immutable; never deleted.
CREATE OR REPLACE FUNCTION parse_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'parse_runs rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.document_version_id, NEW.parser_name, NEW.package_version,
        NEW.output_schema, NEW.model_revision, NEW.requested_profile, NEW.effective_profile,
        NEW.fallback_reason, NEW.raw_artifact_id, NEW.manifest_artifact_id, NEW.page_index_map,
        NEW.cost_usd, NEW.operation_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.document_version_id, OLD.parser_name, OLD.package_version,
        OLD.output_schema, OLD.model_revision, OLD.requested_profile, OLD.effective_profile,
        OLD.fallback_reason, OLD.raw_artifact_id, OLD.manifest_artifact_id, OLD.page_index_map,
        OLD.cost_usd, OLD.operation_id, OLD.created_at) THEN
        RAISE EXCEPTION 'parse_run identity columns are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NOT (OLD.status = 'running'
            AND NEW.status IN ('succeeded', 'partial', 'needs_review', 'failed')) THEN
        RAISE EXCEPTION 'illegal parse_run transition % -> % (id %)', OLD.status, NEW.status, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER parse_runs_guard
    BEFORE UPDATE OR DELETE ON parse_runs
    FOR EACH ROW EXECUTE FUNCTION parse_runs_guard();

-- visual_objects: identity columns immutable; status transitions limited to
-- extracted -> verified/needs_review/rejected and needs_review -> verified/rejected;
-- verified and rejected are terminal.
CREATE OR REPLACE FUNCTION visual_objects_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'visual_objects rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.parse_run_id, NEW.evidence_unit_id, NEW.object_type,
        NEW.parent_id, NEW.crop_artifact_id, NEW.context_artifact_id, NEW.transform, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.parse_run_id, OLD.evidence_unit_id, OLD.object_type,
        OLD.parent_id, OLD.crop_artifact_id, OLD.context_artifact_id, OLD.transform, OLD.created_at) THEN
        RAISE EXCEPTION 'visual_object identity columns are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NOT ((OLD.status = 'extracted' AND NEW.status IN ('verified', 'needs_review', 'rejected'))
            OR (OLD.status = 'needs_review' AND NEW.status IN ('verified', 'rejected'))) THEN
        RAISE EXCEPTION 'illegal visual_object transition % -> % (id %)', OLD.status, NEW.status, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER visual_objects_guard
    BEFORE UPDATE OR DELETE ON visual_objects
    FOR EACH ROW EXECUTE FUNCTION visual_objects_guard();

-- derivations: once verified, no column may change; never deleted.
CREATE OR REPLACE FUNCTION derivations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'derivations rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'verified' THEN
        RAISE EXCEPTION 'verified derivation % is immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.layer, NEW.kind, NEW.formula, NEW.model,
        NEW.prompt_revision, NEW.output_claims, NEW.operation_id, NEW.resource_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.layer, OLD.kind, OLD.formula, OLD.model,
        OLD.prompt_revision, OLD.output_claims, OLD.operation_id, OLD.resource_id, OLD.created_at) THEN
        RAISE EXCEPTION 'derivation identity columns are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER derivations_guard
    BEFORE UPDATE OR DELETE ON derivations
    FOR EACH ROW EXECUTE FUNCTION derivations_guard();

-- ============================================================================
-- 2. evidence_regions must belong to the same document_version_id as evidence_unit
-- ============================================================================

CREATE OR REPLACE FUNCTION evidence_regions_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.document_version_id IS DISTINCT FROM (
        SELECT document_version_id FROM evidence_units WHERE id = NEW.evidence_unit_id
    ) THEN
        RAISE EXCEPTION 'evidence_region document_version_id % does not match evidence_unit %',
            NEW.document_version_id, NEW.evidence_unit_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER evidence_regions_document_check
    BEFORE INSERT OR UPDATE ON evidence_regions
    FOR EACH ROW EXECUTE FUNCTION evidence_regions_document_guard();

-- ============================================================================
-- 3. Artifact references on submissions (and node_operations handled above)
-- ============================================================================

CREATE OR REPLACE FUNCTION submissions_artifact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_run_project_id uuid;
    v_art_project_id uuid;
    v_art_state      artifact_state;
BEGIN
    SELECT project_id INTO v_run_project_id FROM runs WHERE id = NEW.run_id;
    SELECT project_id, state INTO v_art_project_id, v_art_state
    FROM artifacts WHERE id = NEW.payload_artifact_id;

    IF v_art_project_id IS DISTINCT FROM v_run_project_id
       OR v_art_state IS DISTINCT FROM 'committed'::artifact_state THEN
        RAISE EXCEPTION 'submission payload_artifact_id % must belong to run project and be committed',
            NEW.payload_artifact_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER submissions_artifact_check
    BEFORE INSERT ON submissions
    FOR EACH ROW EXECUTE FUNCTION submissions_artifact_guard();

-- ============================================================================
-- 4. Strengthened artifacts_guard
-- ============================================================================

ALTER TABLE artifacts
    ADD CONSTRAINT artifacts_tombstone_reason_only_when_tombstoned
    CHECK (state = 'tombstoned' OR tombstone_reason IS NULL);

ALTER TABLE artifacts
    ADD CONSTRAINT artifacts_no_legal_hold_tombstone
    CHECK (NOT (retention = 'legal_hold' AND state = 'tombstoned'));

CREATE OR REPLACE FUNCTION artifacts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.retention = 'legal_hold' THEN
            RAISE EXCEPTION 'artifact % is under legal_hold and cannot be deleted', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.state <> 'staged' THEN
            RAISE EXCEPTION 'artifact % is %; tombstone it instead of deleting', OLD.id, OLD.state
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF (NEW.id, NEW.project_id, NEW.object_key, NEW.object_version, NEW.sha256,
        NEW.size_bytes, NEW.mime, NEW.retention, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.object_key, OLD.object_version, OLD.sha256,
        OLD.size_bytes, OLD.mime, OLD.retention, OLD.created_at) THEN
        RAISE EXCEPTION 'artifact % identity and retention are immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.state = 'tombstoned' THEN
        RAISE EXCEPTION 'tombstoned artifact % is final', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.state = 'staged' AND NEW.state = 'committed' THEN
        IF OLD.committed_at IS NOT NULL OR NEW.committed_at IS NULL THEN
            RAISE EXCEPTION 'committed_at must transition from NULL to non-NULL on staged -> committed (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.tombstoned_at IS NOT NULL OR NEW.tombstone_reason IS NOT NULL THEN
            RAISE EXCEPTION 'tombstoned_at and tombstone_reason may only be set on committed -> tombstoned (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.state = 'committed' AND NEW.state = 'tombstoned' THEN
        IF OLD.retention = 'legal_hold' THEN
            RAISE EXCEPTION 'artifact % is under legal_hold and cannot be tombstoned', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.committed_at IS DISTINCT FROM OLD.committed_at THEN
            RAISE EXCEPTION 'committed_at cannot change after commit (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.tombstoned_at IS NULL OR NEW.tombstone_reason IS NULL THEN
            RAISE EXCEPTION 'tombstoned_at and tombstone_reason are required on committed -> tombstoned (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.state = NEW.state THEN
        IF NEW.committed_at IS DISTINCT FROM OLD.committed_at THEN
            RAISE EXCEPTION 'committed_at may only be set during staged -> committed (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at
           OR NEW.tombstone_reason IS DISTINCT FROM OLD.tombstone_reason THEN
            RAISE EXCEPTION 'tombstone fields may only be set during committed -> tombstoned (artifact %)', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'illegal artifact transition % -> %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END $$;

-- ============================================================================
-- 5. evaluations insert & publish guard with concurrency locking
-- ============================================================================

CREATE OR REPLACE FUNCTION evaluations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'provisional' THEN
            RAISE EXCEPTION 'new evaluation % must start with status provisional', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' OR OLD.status = 'published' THEN
        RAISE EXCEPTION 'evaluation % cannot change', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.run_id, NEW.benchmark_id, NEW.grader_revision, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.benchmark_id, OLD.grader_revision, OLD.created_at) THEN
        RAISE EXCEPTION 'evaluation identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = 'published' THEN
        PERFORM 1 FROM benchmark_revisions WHERE id = NEW.benchmark_id FOR SHARE;
        PERFORM 1 FROM evaluations WHERE id = NEW.id FOR SHARE;

        IF NOT EXISTS (
            SELECT 1 FROM questions q WHERE q.benchmark_id = NEW.benchmark_id
        ) THEN
            RAISE EXCEPTION 'cannot publish evaluation % for benchmark % with no questions', NEW.id, NEW.benchmark_id
                USING ERRCODE = 'check_violation';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM questions q
            WHERE q.benchmark_id = NEW.benchmark_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM question_scores qs
                  WHERE qs.evaluation_id = NEW.id
                    AND qs.question_id = q.id
              )
        ) THEN
            RAISE EXCEPTION 'every benchmark question must have at least one question_score before publishing evaluation %', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;

        IF EXISTS (
            SELECT 1 FROM question_scores WHERE evaluation_id = NEW.id AND status = 'pending'
        ) OR EXISTS (
            SELECT 1 FROM fact_scores WHERE evaluation_id = NEW.id AND status = 'pending'
        ) THEN
            RAISE EXCEPTION 'pending scores block publishing (SPEC 16.5)'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER evaluations_guard ON evaluations;
CREATE TRIGGER evaluations_guard
    BEFORE INSERT OR UPDATE OR DELETE ON evaluations
    FOR EACH ROW EXECUTE FUNCTION evaluations_guard();

-- ============================================================================
-- 6. question_scores accepted submission enforcement & FOR SHARE guard
-- ============================================================================

CREATE OR REPLACE FUNCTION question_scores_submission() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_accepted_id uuid;
BEGIN
    SELECT s.id INTO v_accepted_id
    FROM submissions s
    WHERE s.run_id = NEW.run_id
      AND s.question_id = NEW.question_id
      AND s.status = 'accepted';

    IF v_accepted_id IS NOT NULL THEN
        IF NEW.submission_id IS DISTINCT FROM v_accepted_id THEN
            RAISE EXCEPTION 'score must reference the accepted submission % of its (run, question)', v_accepted_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    ELSE
        IF NEW.submission_id IS NOT NULL THEN
            RAISE EXCEPTION 'score must reference the accepted submission of its question'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END IF;

    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION scores_of_published_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE e uuid;
BEGIN
    e := CASE WHEN TG_OP = 'DELETE' THEN OLD.evaluation_id ELSE NEW.evaluation_id END;
    IF (SELECT status FROM evaluations WHERE id = e FOR SHARE) = 'published' THEN
        RAISE EXCEPTION 'evaluation % is published', e
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

-- ============================================================================
-- 7. runs state machine and transition history
-- ============================================================================

CREATE TABLE run_transitions_allowed (
    from_status run_status NOT NULL,
    to_status   run_status NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

INSERT INTO run_transitions_allowed (from_status, to_status) VALUES
    ('queued', 'preparing'),
    ('queued', 'cancelled'),
    ('preparing', 'running'),
    ('preparing', 'awaiting_submissions'),
    ('preparing', 'failed'),
    ('preparing', 'cancelled'),
    ('preparing', 'paused_budget'),
    ('running', 'evaluating'),
    ('running', 'failed'),
    ('running', 'cancelled'),
    ('running', 'paused_budget'),
    ('running', 'completed_with_errors'),
    ('awaiting_submissions', 'evaluating'),
    ('awaiting_submissions', 'cancelled'),
    ('awaiting_submissions', 'failed'),
    ('evaluating', 'completed'),
    ('evaluating', 'completed_with_errors'),
    ('evaluating', 'review_required'),
    ('evaluating', 'failed'),
    ('review_required', 'evaluating'),
    ('review_required', 'completed'),
    ('review_required', 'cancelled'),
    ('paused_budget', 'preparing'),
    ('paused_budget', 'running'),
    ('paused_budget', 'cancelled');

CREATE TABLE run_transitions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    from_status run_status NOT NULL,
    to_status   run_status NOT NULL,
    row_version integer NOT NULL,
    reason      text NOT NULL CHECK (length(btrim(reason)) >= 1),
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER run_transitions_append_only
    BEFORE UPDATE OR DELETE ON run_transitions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE OR REPLACE FUNCTION runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'runs are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status IN ('completed', 'completed_with_errors', 'cancelled', 'failed') THEN
        RAISE EXCEPTION 'run % is in terminal state %', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.benchmark_id, NEW.method_revision_id, NEW.budget_profile)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.benchmark_id, OLD.method_revision_id, OLD.budget_profile) THEN
        RAISE EXCEPTION 'run identity and budget_profile are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status AND NOT EXISTS (
        SELECT 1 FROM run_transitions_allowed
        WHERE from_status = OLD.status AND to_status = NEW.status
    ) THEN
        RAISE EXCEPTION 'illegal run transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO run_transitions (run_id, from_status, to_status, row_version, reason)
        VALUES (
            NEW.id,
            OLD.status,
            NEW.status,
            NEW.row_version,
            COALESCE(NULLIF(current_setting('lab.transition_reason', true), ''), 'unspecified')
        );
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER runs_guard
    BEFORE UPDATE OR DELETE ON runs
    FOR EACH ROW EXECUTE FUNCTION runs_guard();

-- ============================================================================
-- 8. Benchmark transition logging, freeze question check, resource sync, FOR SHARE
-- ============================================================================

CREATE OR REPLACE FUNCTION benchmark_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_actor_text text;
    v_actor_id   uuid;
    v_reason     text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark revisions are never deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'frozen' THEN
        RAISE EXCEPTION 'benchmark revision % is frozen', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.resource_id, NEW.document_version_id, NEW.n_target, NEW.q_min)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.resource_id, OLD.document_version_id, OLD.n_target, OLD.q_min) THEN
        RAISE EXCEPTION 'benchmark identity/targets are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (
            SELECT 1 FROM benchmark_transitions_allowed
            WHERE from_status = OLD.status AND to_status = NEW.status
        ) THEN
            RAISE EXCEPTION 'illegal benchmark transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = 'frozen' THEN
        IF NOT EXISTS (SELECT 1 FROM questions WHERE benchmark_id = NEW.id) THEN
            RAISE EXCEPTION 'cannot freeze benchmark revision % without questions', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;

        UPDATE resource_versions
        SET frozen = true
        WHERE project_id = NEW.project_id
          AND id = NEW.resource_id
          AND NOT frozen;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        v_actor_text := NULLIF(current_setting('lab.actor_id', true), '');
        IF v_actor_text IS NULL THEN
            RAISE EXCEPTION 'lab.actor_id must be set for benchmark status transition'
                USING ERRCODE = 'check_violation';
        END IF;
        v_actor_id := v_actor_text::uuid;
        v_reason := COALESCE(NULLIF(current_setting('lab.transition_reason', true), ''), 'unspecified');

        INSERT INTO benchmark_transitions (benchmark_id, from_status, to_status, row_version, reason, actor_id)
        VALUES (NEW.id, OLD.status, NEW.status, NEW.row_version, v_reason, v_actor_id);
    END IF;

    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION benchmark_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b uuid;
BEGIN
    b := CASE WHEN TG_OP = 'DELETE' THEN OLD.benchmark_id ELSE NEW.benchmark_id END;
    IF (SELECT status FROM benchmark_revisions WHERE id = b FOR SHARE) = 'frozen'
       OR (TG_OP = 'UPDATE' AND (SELECT status FROM benchmark_revisions WHERE id = OLD.benchmark_id FOR SHARE) = 'frozen') THEN
        RAISE EXCEPTION '% of frozen benchmark % cannot change', TG_TABLE_NAME, b
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

-- ============================================================================
-- 9. Strengthened jobs_guard & max_lease_token tracking
-- ============================================================================

ALTER TABLE jobs ADD COLUMN max_lease_token bigint;
UPDATE jobs SET max_lease_token = lease_token WHERE lease_token IS NOT NULL;

ALTER TABLE jobs
    ADD CONSTRAINT jobs_attempt_within_max
    CHECK (attempt <= max_attempts);

CREATE OR REPLACE FUNCTION jobs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.attempt > NEW.max_attempts THEN
            RAISE EXCEPTION 'job attempt % exceeds max_attempts %', NEW.attempt, NEW.max_attempts
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.max_lease_token := NEW.lease_token;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'jobs are never deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status IN ('succeeded', 'cancelled') THEN
        RAISE EXCEPTION 'job % is terminal (%); create a new job', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.kind, NEW.max_attempts, NEW.retryable)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.kind, OLD.max_attempts, OLD.retryable) THEN
        RAISE EXCEPTION 'job identity and retry configuration are immutable (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status AND NOT EXISTS (
        SELECT 1 FROM job_transitions_allowed
        WHERE from_status = OLD.status AND to_status = NEW.status
    ) THEN
        RAISE EXCEPTION 'illegal job transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'failed' AND NEW.status = 'queued'
       AND NOT (OLD.retryable AND OLD.attempt < OLD.max_attempts) THEN
        RAISE EXCEPTION 'job % is not retryable or has no attempts left', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.attempt NOT IN (OLD.attempt, OLD.attempt + 1)
       OR NEW.attempt > NEW.max_attempts THEN
        RAISE EXCEPTION 'job attempt must stay unchanged or increment by 1 and not exceed max_attempts'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.lease_token IS NOT NULL THEN
        IF NEW.lease_token IS DISTINCT FROM OLD.lease_token THEN
            IF OLD.max_lease_token IS NOT NULL AND NEW.lease_token <= OLD.max_lease_token THEN
                RAISE EXCEPTION 'fencing token % must exceed historical max_lease_token %',
                    NEW.lease_token, OLD.max_lease_token
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            NEW.max_lease_token := NEW.lease_token;
        ELSE
            IF OLD.max_lease_token IS NOT NULL AND NEW.lease_token < OLD.max_lease_token THEN
                RAISE EXCEPTION 'fencing token may not go backwards'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            NEW.max_lease_token := COALESCE(OLD.max_lease_token, NEW.lease_token);
        END IF;
    ELSE
        NEW.max_lease_token := OLD.max_lease_token;
    END IF;

    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status
       OR (NEW.lease_token IS NOT NULL AND NEW.lease_token IS DISTINCT FROM OLD.lease_token) THEN
        INSERT INTO job_transitions (job_id, from_status, to_status, lease_token, reason)
        VALUES (
            NEW.id,
            OLD.status,
            NEW.status,
            COALESCE(NEW.lease_token, OLD.lease_token),
            COALESCE(NULLIF(current_setting('lab.transition_reason', true), ''), 'unspecified')
        );
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER jobs_guard ON jobs;
CREATE TRIGGER jobs_guard
    BEFORE INSERT OR UPDATE OR DELETE ON jobs
    FOR EACH ROW EXECUTE FUNCTION jobs_guard();

-- ============================================================================
-- 10. Job-scoped uniqueness on node_operations
-- ============================================================================

DO $$
DECLARE
    v_conname text;
BEGIN
    FOR v_conname IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = current_schema()
          AND t.relname = 'node_operations'
          AND c.contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE node_operations DROP CONSTRAINT %I', v_conname);
    END LOOP;
END $$;

DROP INDEX IF EXISTS node_operations_one_success;

ALTER TABLE node_operations
    ADD CONSTRAINT node_operations_job_invocation_attempt_key
    UNIQUE (job_id, work_item_id, node_revision, logical_invocation_id, attempt);

CREATE UNIQUE INDEX node_operations_one_success
    ON node_operations (job_id, work_item_id, node_revision, logical_invocation_id)
    WHERE status = 'succeeded';

-- ============================================================================
-- 11. Numeric NaN / Infinity guards
-- ============================================================================

ALTER TABLE pages
    ADD CONSTRAINT pages_dimensions_not_nan
    CHECK (width_pt <> 'NaN'::numeric AND height_pt <> 'NaN'::numeric);

ALTER TABLE parse_runs
    ADD CONSTRAINT parse_runs_cost_not_nan
    CHECK (cost_usd IS NULL OR cost_usd <> 'NaN'::numeric);

ALTER TABLE visual_observations
    ADD CONSTRAINT visual_observations_estimates_not_nan
    CHECK (
        (estimate_low IS NULL OR estimate_low <> 'NaN'::numeric)
        AND (estimate_high IS NULL OR estimate_high <> 'NaN'::numeric)
    );

ALTER TABLE question_scores
    ADD CONSTRAINT question_scores_numbers_finite
    CHECK (
        (raw_value IS NULL OR (
            raw_value <> 'NaN'::double precision
            AND raw_value <> 'Infinity'::double precision
            AND raw_value <> '-Infinity'::double precision
        ))
        AND (numerator IS NULL OR (
            numerator <> 'NaN'::double precision
            AND numerator <> 'Infinity'::double precision
            AND numerator <> '-Infinity'::double precision
        ))
        AND (denominator IS NULL OR (
            denominator <> 'NaN'::double precision
            AND denominator <> 'Infinity'::double precision
            AND denominator <> '-Infinity'::double precision
        ))
    );
