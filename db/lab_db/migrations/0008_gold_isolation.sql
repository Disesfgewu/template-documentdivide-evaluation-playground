-- 0008_gold_isolation.sql
-- Row Level Security (RLS) gold isolation for benchmark answers & evidence (SPEC 16.4, A18).

CREATE OR REPLACE FUNCTION lab_can_read_gold(project uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    raw_actor text;
    v_actor   uuid;
BEGIN
    raw_actor := NULLIF(current_setting('lab.actor_id', true), '');
    IF raw_actor IS NULL OR project IS NULL THEN
        RETURN false;
    END IF;
    BEGIN
        v_actor := raw_actor::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN false;
    END;
    RETURN EXISTS (
        SELECT 1
        FROM project_members pm
        WHERE pm.project_id = project
          AND pm.actor_id = v_actor
          AND pm.role IN ('owner', 'maintainer', 'reviewer')
    );
END;
$$;

CREATE OR REPLACE FUNCTION lab_can_write_gold(project uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    raw_actor text;
    v_actor   uuid;
BEGIN
    raw_actor := NULLIF(current_setting('lab.actor_id', true), '');
    IF raw_actor IS NULL OR project IS NULL THEN
        RETURN false;
    END IF;
    BEGIN
        v_actor := raw_actor::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN false;
    END;
    RETURN EXISTS (
        SELECT 1
        FROM project_members pm
        WHERE pm.project_id = project
          AND pm.actor_id = v_actor
          AND pm.role IN ('owner', 'maintainer')
    );
END;
$$;

GRANT EXECUTE ON FUNCTION lab_can_read_gold(uuid) TO PUBLIC;
GRANT EXECUTE ON FUNCTION lab_can_write_gold(uuid) TO PUBLIC;

-- Ensure existing benchmark/evidence integrity trigger functions (from 0002)
-- run as SECURITY DEFINER with fixed search_path so RLS does not block internal checks.
DO $$
DECLARE
    fn record;
BEGIN
    FOR fn IN
        SELECT DISTINCT p.oid::regprocedure AS sig
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN pg_trigger t ON t.tgfoid = p.oid
        WHERE n.nspname = 'public'
          AND (
              p.proname IN ('benchmark_child_guard', 'evidence_from_benchmark_document')
              OR t.tgrelid IN (
                  'public.benchmark_revisions'::regclass,
                  'public.questions'::regclass,
                  'public.facts'::regclass,
                  'public.evidence_sets'::regclass,
                  'public.evidence_set_members'::regclass,
                  'public.distractors'::regclass
              )
          )
    LOOP
        EXECUTE format(
            'ALTER FUNCTION %s SECURITY DEFINER SET search_path = public, pg_temp',
            fn.sig
        );
    END LOOP;
END;
$$;

-- fact_scores_partial_rule (0003) runs on fact_scores but reads facts to see
-- whether a partial-credit rule exists; a grader without gold read access
-- must not make that check silently see "no rule".
ALTER FUNCTION fact_scores_partial_rule() SECURITY DEFINER SET search_path = public, pg_temp;

-- facts RLS
ALTER TABLE facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE facts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS facts_select_gold ON facts;
CREATE POLICY facts_select_gold ON facts
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = facts.benchmark_id)));

DROP POLICY IF EXISTS facts_insert_gold ON facts;
CREATE POLICY facts_insert_gold ON facts
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = facts.benchmark_id)));

DROP POLICY IF EXISTS facts_update_gold ON facts;
CREATE POLICY facts_update_gold ON facts
    FOR UPDATE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = facts.benchmark_id)))
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = facts.benchmark_id)));

DROP POLICY IF EXISTS facts_delete_gold ON facts;
CREATE POLICY facts_delete_gold ON facts
    FOR DELETE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = facts.benchmark_id)));

-- evidence_sets RLS
ALTER TABLE evidence_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_sets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS evidence_sets_select_gold ON evidence_sets;
CREATE POLICY evidence_sets_select_gold ON evidence_sets
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_sets.benchmark_id)));

DROP POLICY IF EXISTS evidence_sets_insert_gold ON evidence_sets;
CREATE POLICY evidence_sets_insert_gold ON evidence_sets
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_sets.benchmark_id)));

DROP POLICY IF EXISTS evidence_sets_update_gold ON evidence_sets;
CREATE POLICY evidence_sets_update_gold ON evidence_sets
    FOR UPDATE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_sets.benchmark_id)))
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_sets.benchmark_id)));

DROP POLICY IF EXISTS evidence_sets_delete_gold ON evidence_sets;
CREATE POLICY evidence_sets_delete_gold ON evidence_sets
    FOR DELETE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_sets.benchmark_id)));

-- evidence_set_members RLS
ALTER TABLE evidence_set_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_set_members FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS evidence_set_members_select_gold ON evidence_set_members;
CREATE POLICY evidence_set_members_select_gold ON evidence_set_members
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_set_members.benchmark_id)));

DROP POLICY IF EXISTS evidence_set_members_insert_gold ON evidence_set_members;
CREATE POLICY evidence_set_members_insert_gold ON evidence_set_members
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_set_members.benchmark_id)));

DROP POLICY IF EXISTS evidence_set_members_update_gold ON evidence_set_members;
CREATE POLICY evidence_set_members_update_gold ON evidence_set_members
    FOR UPDATE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_set_members.benchmark_id)))
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_set_members.benchmark_id)));

DROP POLICY IF EXISTS evidence_set_members_delete_gold ON evidence_set_members;
CREATE POLICY evidence_set_members_delete_gold ON evidence_set_members
    FOR DELETE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = evidence_set_members.benchmark_id)));

-- distractors RLS
ALTER TABLE distractors ENABLE ROW LEVEL SECURITY;
ALTER TABLE distractors FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS distractors_select_gold ON distractors;
CREATE POLICY distractors_select_gold ON distractors
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = distractors.benchmark_id)));

DROP POLICY IF EXISTS distractors_insert_gold ON distractors;
CREATE POLICY distractors_insert_gold ON distractors
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = distractors.benchmark_id)));

DROP POLICY IF EXISTS distractors_update_gold ON distractors;
CREATE POLICY distractors_update_gold ON distractors
    FOR UPDATE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = distractors.benchmark_id)))
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = distractors.benchmark_id)));

DROP POLICY IF EXISTS distractors_delete_gold ON distractors;
CREATE POLICY distractors_delete_gold ON distractors
    FOR DELETE
    USING (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = distractors.benchmark_id)));
