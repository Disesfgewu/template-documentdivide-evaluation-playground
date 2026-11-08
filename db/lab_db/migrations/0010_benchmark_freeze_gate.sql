-- 0010: fail-closed benchmark freeze gate and safe draft initialization (SPEC 10, R05, R06, issue #10).
--
-- Direct INSERT into benchmark_revisions must begin in 'draft' status with
-- row_version 1, null frozen_at, and null compliance.
-- Direct UPDATE into 'frozen' status is rejected for both compliant and partial
-- benchmarks until mandatory eligibility (independent question validation,
-- human review gates, and verified quotas) is implemented in persistent schema.
-- No app-accessible bypass.
-- Legacy frozen fixtures in existing test suites are preserved via a restricted,
-- test-only setting accessible only to non-app database owners.
--
-- Migration version collision coordination:
-- Note: PR #74 (issue-11-pgvector-index) concurrently introduces 0010_vectors.sql,
-- and PR #77 introduces Alembic version tracking. Gap-free 1..n numbering is enforced
-- by db/lab_db/migrate.py. The first PR to merge keeps 0010; subsequent branches
-- must renumber (e.g. to 0011) or reconcile with Alembic revisions upon merge.

DROP TRIGGER IF EXISTS benchmark_revisions_guard ON benchmark_revisions;

CREATE OR REPLACE FUNCTION benchmark_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_actor_text text;
    v_actor_id   uuid;
    v_reason     text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'new benchmark revisions must begin in draft status'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.row_version <> 1 THEN
            RAISE EXCEPTION 'new benchmark revisions must begin at row_version 1'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.frozen_at IS NOT NULL THEN
            RAISE EXCEPTION 'new benchmark revisions cannot have frozen_at set'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.compliance IS NOT NULL THEN
            RAISE EXCEPTION 'new benchmark revisions cannot have compliance set'
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END IF;

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

    IF NEW.status = 'frozen' AND OLD.status <> 'frozen' THEN
        IF current_setting('lab.test_allow_legacy_freeze', true) = 'true' THEN
            IF current_user LIKE 'lab_app_%' OR session_user LIKE 'lab_app_%' THEN
                RAISE EXCEPTION 'app role cannot bypass benchmark freeze gate'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            IF NOT EXISTS (SELECT 1 FROM questions WHERE benchmark_id = NEW.id) THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision % without questions', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSE
            RAISE EXCEPTION 'cannot freeze benchmark revision %: mandatory eligibility verification is not implemented in current schema', NEW.id
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

CREATE TRIGGER benchmark_revisions_guard
    BEFORE INSERT OR UPDATE OR DELETE ON benchmark_revisions
    FOR EACH ROW EXECUTE FUNCTION benchmark_revisions_guard();
