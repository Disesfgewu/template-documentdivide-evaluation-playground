-- 0017: the benchmark freeze gate (PM-DECISION 2026-11-12 item 2; reconciles the draft PR #79, Refs #10).
--
-- Before this migration any role with the transition right could move a benchmark ready_to_freeze -> frozen with nothing
-- but "has a question". Now a freeze needs, enforced HERE in the database for every writer:
--   * the acting user (lab.actor_id) is an OWNER of the project (SPEC 5.1/5.3);
--   * compliance = 'partial': 'compliant' cannot be proven (human review is skipped by the owner's scope, D6, and the
--     quota buckets are incomplete), so it is refused;
--   * at least one question, and EVERY question's latest automated validation (question_validations, 0016) is 'pass'.
--     A question with a fail, needs_review or no validation blocks the freeze: nothing is excluded or deleted (a frozen set
--     is immutable and children are never removed), so the owner must resolve it before freezing.
-- The only way into ready_to_freeze is from validating (benchmark_transitions_allowed, 0002), and the freeze itself is only
-- ready_to_freeze -> frozen, so validating -> ready_to_freeze -> frozen is enforced by the transition table for every writer.
-- 'Latest validation' is the newest row by (created_at, id): rows of one transaction share created_at, id breaks the tie.
-- Also from #79: a benchmark revision is only ever INSERTED as a fresh draft (status draft, row_version 1, no frozen_at,
-- no compliance), so a pre-frozen row cannot be inserted around the gate.
-- Test fixtures that need a frozen benchmark without validations can use the superuser-only setting
-- lab.test_allow_legacy_freeze (session and effective role must both be superusers; application roles cannot).
-- The function is SECURITY DEFINER with a fixed search_path (as in 0008) so it can read the gold-protected
-- question_validations whatever role performs the update.

DROP TRIGGER IF EXISTS benchmark_revisions_guard ON benchmark_revisions;

CREATE OR REPLACE FUNCTION benchmark_revisions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_actor_text text;
    v_actor_id   uuid;
    v_reason     text;
    v_bad        bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'new benchmark revisions must begin in draft status' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.row_version <> 1 THEN
            RAISE EXCEPTION 'new benchmark revisions must begin at row_version 1' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.frozen_at IS NOT NULL THEN
            RAISE EXCEPTION 'new benchmark revisions cannot have frozen_at set' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.compliance IS NOT NULL THEN
            RAISE EXCEPTION 'new benchmark revisions cannot have compliance set' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark revisions are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'frozen' THEN
        RAISE EXCEPTION 'benchmark revision % is frozen', OLD.id USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.id, NEW.project_id, NEW.resource_id, NEW.document_version_id, NEW.n_target, NEW.q_min)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.resource_id, OLD.document_version_id, OLD.n_target, OLD.q_min) THEN
        RAISE EXCEPTION 'benchmark identity/targets are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (
            SELECT 1 FROM benchmark_transitions_allowed WHERE from_status = OLD.status AND to_status = NEW.status
        ) THEN
            RAISE EXCEPTION 'illegal benchmark transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = 'frozen' THEN
        IF NOT EXISTS (SELECT 1 FROM questions WHERE benchmark_id = NEW.id) THEN
            RAISE EXCEPTION 'cannot freeze benchmark revision % without questions', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF current_setting('lab.test_allow_legacy_freeze', true) = 'true' THEN
            -- this function runs as its owner (SECURITY DEFINER): the CALLER's identity is session_user plus the role set
            -- with SET ROLE (current_setting('role')); both must be superusers
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = session_user AND rolsuper)
               OR (current_setting('role', true) NOT IN ('none', '')
                   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = current_setting('role', true) AND rolsuper)) THEN
                RAISE EXCEPTION 'only superuser sessions and roles may bypass the benchmark freeze gate'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        ELSE
            v_actor_text := NULLIF(current_setting('lab.actor_id', true), '');
            IF v_actor_text IS NULL THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision %: lab.actor_id must be set', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            v_actor_id := v_actor_text::uuid;
            IF NOT EXISTS (SELECT 1 FROM project_members
                           WHERE project_id = NEW.project_id AND actor_id = v_actor_id AND role = 'owner') THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision %: only a project owner may freeze', NEW.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.compliance IS DISTINCT FROM 'partial' THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision %: compliance must be partial (human review is skipped and the quota buckets are incomplete; compliant cannot be proven)', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT count(*) INTO v_bad FROM questions q
            WHERE q.benchmark_id = NEW.id AND NOT EXISTS (
                SELECT 1 FROM (SELECT DISTINCT ON (v.question_id) v.verdict FROM question_validations v
                               WHERE v.benchmark_id = NEW.id AND v.question_id = q.id
                               ORDER BY v.question_id, v.created_at DESC, v.id DESC) latest
                WHERE latest.verdict = 'pass');
            IF v_bad > 0 THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision %: % question(s) lack a passing automated validation', NEW.id, v_bad
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;

        UPDATE resource_versions SET frozen = true
        WHERE project_id = NEW.project_id AND id = NEW.resource_id AND NOT frozen;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        v_actor_text := NULLIF(current_setting('lab.actor_id', true), '');
        IF v_actor_text IS NULL THEN
            RAISE EXCEPTION 'lab.actor_id must be set for benchmark status transition' USING ERRCODE = 'check_violation';
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
