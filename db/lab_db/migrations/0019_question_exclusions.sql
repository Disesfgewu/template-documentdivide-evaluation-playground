-- 0019: the owner can EXCLUDE a question from a draft benchmark before the freeze (PM-DECISION on Issue #7, Refs #10/#12).
--
-- Real output showed the freeze rule "every question must pass" is unreachable in practice (3 of 10 real questions failed their
-- automated validation and the only way forward was to regenerate). An exclusion is a RECORDED decision, never a delete:
--   * question_exclusions is append-only; a row says 'exclude' or 'restore' with the owner's reason, the latest row per question
--     wins; it can only be written while the benchmark is 'validating' (never on a frozen one);
--   * active_questions is the view of the questions whose latest decision is not 'exclude'. Excluded questions are not frozen
--     into the scored set, not answered and not scored: every reader of "the benchmark's questions" uses the view;
--   * the freeze gate (the guard below, replacing 0017's function) now requires at least one ACTIVE question and a passing latest
--     automated validation for EVERY active question.
-- Who may exclude (the owner) is decided by the API and ALSO here: the acting user (lab.actor_id) must be a project owner.

CREATE TABLE question_exclusions (
    id            uuid PRIMARY KEY,
    seq           bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    benchmark_id  uuid NOT NULL,
    question_id   uuid NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('exclude', 'restore')),
    reason        text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    actor_id      uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);
CREATE INDEX question_exclusions_question_idx ON question_exclusions (question_id, seq DESC);
CREATE INDEX question_exclusions_benchmark_idx ON question_exclusions (benchmark_id);
CREATE TRIGGER question_exclusions_append_only BEFORE UPDATE OR DELETE ON question_exclusions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE FUNCTION question_exclusions_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_status text;
    v_project uuid;
BEGIN
    SELECT status::text, project_id INTO v_status, v_project FROM benchmark_revisions WHERE id = NEW.benchmark_id FOR SHARE;
    IF v_status IS DISTINCT FROM 'validating' THEN
        RAISE EXCEPTION 'questions can only be excluded or restored while the benchmark is validating (it is %)', COALESCE(v_status, 'missing')
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM project_members WHERE project_id = v_project AND actor_id = NEW.actor_id AND role = 'owner') THEN
        RAISE EXCEPTION 'only a project owner may exclude or restore a question' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER question_exclusions_guard BEFORE INSERT ON question_exclusions
    FOR EACH ROW EXECUTE FUNCTION question_exclusions_guard();

CREATE VIEW active_questions AS
SELECT q.* FROM questions q
WHERE NOT EXISTS (
    SELECT 1 FROM question_exclusions e
    WHERE e.question_id = q.id AND e.kind = 'exclude'
      AND e.seq = (SELECT max(e2.seq) FROM question_exclusions e2 WHERE e2.question_id = q.id));

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
        IF NOT EXISTS (SELECT 1 FROM active_questions WHERE benchmark_id = NEW.id) THEN
            RAISE EXCEPTION 'cannot freeze benchmark revision % without questions (every question excluded counts as none)', NEW.id
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
            SELECT count(*) INTO v_bad FROM active_questions q
            WHERE q.benchmark_id = NEW.id AND NOT EXISTS (
                SELECT 1 FROM (SELECT DISTINCT ON (v.question_id) v.verdict FROM question_validations v
                               WHERE v.benchmark_id = NEW.id AND v.question_id = q.id
                               ORDER BY v.question_id, v.created_at DESC, v.id DESC) latest
                WHERE latest.verdict = 'pass');
            IF v_bad > 0 THEN
                RAISE EXCEPTION 'cannot freeze benchmark revision %: % non-excluded question(s) lack a passing automated validation', NEW.id, v_bad
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
