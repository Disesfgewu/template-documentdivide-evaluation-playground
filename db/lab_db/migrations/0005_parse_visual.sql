-- 0005_parse_visual.sql
-- Multimodal parsing, L0-L3 layered visual evidence, cross-page evidence
-- relations, source image verifications, and L3 derivations (SPEC 19.2, 19.6).

CREATE TABLE parse_runs (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id           uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    document_version_id  uuid NOT NULL,
    parser_name          text NOT NULL CHECK (length(btrim(parser_name)) BETWEEN 1 AND 128),
    package_version      text NOT NULL DEFAULT 'unknown' CHECK (length(btrim(package_version)) BETWEEN 1 AND 128),
    output_schema        text NOT NULL CHECK (length(btrim(output_schema)) BETWEEN 1 AND 128),
    model_revision       text CHECK (model_revision IS NULL OR length(btrim(model_revision)) BETWEEN 1 AND 128),
    requested_profile    text NOT NULL CHECK (length(btrim(requested_profile)) BETWEEN 1 AND 128),
    effective_profile    text NOT NULL CHECK (length(btrim(effective_profile)) BETWEEN 1 AND 128),
    fallback_reason      text,
    raw_artifact_id      uuid NOT NULL,
    manifest_artifact_id uuid NOT NULL,
    page_index_map       jsonb NOT NULL DEFAULT '{}'::jsonb,
    cost_usd             numeric CHECK (cost_usd IS NULL OR cost_usd >= 0),
    status               text NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'needs_review', 'failed')),
    operation_id         uuid NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    CHECK (
        (requested_profile = effective_profile AND fallback_reason IS NULL)
        OR (requested_profile <> effective_profile AND fallback_reason IS NOT NULL AND length(btrim(fallback_reason)) > 0)
    ),
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, raw_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, manifest_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_parse_runs_project_doc ON parse_runs (project_id, document_version_id);
CREATE INDEX idx_parse_runs_project_status ON parse_runs (project_id, status);

CREATE TABLE visual_objects (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    parse_run_id        uuid NOT NULL,
    evidence_unit_id    uuid,
    object_type         text NOT NULL CHECK (object_type IN ('chart', 'table', 'image', 'diagram', 'photo', 'formula')),
    parent_id           uuid CHECK (parent_id IS NULL OR parent_id <> id),
    crop_artifact_id    uuid,
    context_artifact_id uuid,
    transform           jsonb NOT NULL DEFAULT '{}'::jsonb,
    status              text NOT NULL DEFAULT 'extracted' CHECK (status IN ('extracted', 'verified', 'needs_review', 'rejected')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    CHECK (status <> 'verified' OR crop_artifact_id IS NOT NULL),
    FOREIGN KEY (project_id, parse_run_id) REFERENCES parse_runs(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, evidence_unit_id) REFERENCES evidence_units(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, parent_id) REFERENCES visual_objects(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, crop_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, context_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_visual_objects_project_parse_run ON visual_objects (project_id, parse_run_id);
CREATE INDEX idx_visual_objects_project_evidence ON visual_objects (project_id, evidence_unit_id);
CREATE INDEX idx_visual_objects_crop_artifact ON visual_objects (project_id, crop_artifact_id);

CREATE FUNCTION visual_objects_verify_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_crop_state artifact_state;
    v_run_doc_id uuid;
    v_ev_doc_id  uuid;
BEGIN
    IF NEW.evidence_unit_id IS NOT NULL THEN
        SELECT pr.document_version_id INTO v_run_doc_id
        FROM parse_runs pr
        WHERE pr.project_id = NEW.project_id AND pr.id = NEW.parse_run_id;

        SELECT eu.document_version_id INTO v_ev_doc_id
        FROM evidence_units eu
        WHERE eu.project_id = NEW.project_id AND eu.id = NEW.evidence_unit_id;

        IF v_run_doc_id IS DISTINCT FROM v_ev_doc_id THEN
            RAISE EXCEPTION 'visual_objects evidence_unit_id must belong to the same document_version_id as parse_run_id'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.status = 'verified' THEN
        IF NEW.crop_artifact_id IS NULL THEN
            RAISE EXCEPTION 'verified visual_object requires non-null crop_artifact_id'
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT a.state INTO v_crop_state
        FROM artifacts a
        WHERE a.project_id = NEW.project_id AND a.id = NEW.crop_artifact_id;

        IF v_crop_state IS DISTINCT FROM 'committed'::artifact_state THEN
            RAISE EXCEPTION 'verified visual_object requires crop artifact in committed state (got %)', v_crop_state
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER visual_objects_verify_check
    AFTER INSERT OR UPDATE ON visual_objects
    FOR EACH ROW EXECUTE FUNCTION visual_objects_verify_guard();

CREATE FUNCTION artifacts_verified_crop_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'committed'::artifact_state AND NEW.state <> 'committed'::artifact_state THEN
        IF EXISTS (
            SELECT 1
            FROM visual_objects vo
            WHERE vo.project_id = OLD.project_id
              AND vo.crop_artifact_id = OLD.id
              AND vo.status = 'verified'
        ) THEN
            RAISE EXCEPTION 'cannot change state of crop artifact referenced by verified visual_object'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER artifacts_verified_crop_check
    BEFORE UPDATE OF state ON artifacts
    FOR EACH ROW EXECUTE FUNCTION artifacts_verified_crop_guard();

CREATE TABLE visual_object_regions (
    visual_object_id    uuid NOT NULL REFERENCES visual_objects(id) ON DELETE RESTRICT,
    ordinal             integer NOT NULL CHECK (ordinal >= 0),
    document_version_id uuid,
    page_index          integer NOT NULL,
    x0                  double precision NOT NULL,
    y0                  double precision NOT NULL,
    x1                  double precision NOT NULL,
    y1                  double precision NOT NULL,
    PRIMARY KEY (visual_object_id, ordinal),
    FOREIGN KEY (document_version_id, page_index) REFERENCES pages(document_version_id, page_index) ON DELETE RESTRICT,
    CHECK (0 <= x0 AND x0 < x1 AND x1 <= 1 AND 0 <= y0 AND y0 < y1 AND y1 <= 1)
);

CREATE FUNCTION visual_object_regions_doc_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.document_version_id IS NULL THEN
        SELECT pr.document_version_id INTO NEW.document_version_id
        FROM visual_objects vo
        JOIN parse_runs pr ON pr.project_id = vo.project_id AND pr.id = vo.parse_run_id
        WHERE vo.id = NEW.visual_object_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER visual_object_regions_set_doc
    BEFORE INSERT OR UPDATE ON visual_object_regions
    FOR EACH ROW EXECUTE FUNCTION visual_object_regions_doc_sync();

CREATE FUNCTION visual_object_regions_doc_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_expected_doc_id uuid;
BEGIN
    SELECT pr.document_version_id INTO v_expected_doc_id
    FROM visual_objects vo
    JOIN parse_runs pr ON pr.project_id = vo.project_id AND pr.id = vo.parse_run_id
    WHERE vo.id = NEW.visual_object_id;

    IF v_expected_doc_id IS DISTINCT FROM NEW.document_version_id THEN
        RAISE EXCEPTION 'visual_object_regions document_version_id (%) does not match visual_object document_version_id (%)',
            NEW.document_version_id, v_expected_doc_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER visual_object_regions_doc_check
    AFTER INSERT OR UPDATE ON visual_object_regions
    FOR EACH ROW EXECUTE FUNCTION visual_object_regions_doc_guard();

CREATE TABLE visual_observations (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    visual_object_id      uuid NOT NULL,
    layer                 text NOT NULL CHECK (layer IN ('L1', 'L2')),
    observation_type      text NOT NULL CHECK (length(btrim(observation_type)) BETWEEN 1 AND 128),
    data_artifact_id      uuid NOT NULL,
    value_source          text CHECK (value_source IN ('observed_label', 'estimated_from_plot', 'derived_calculation')),
    estimate_low          numeric,
    estimate_high         numeric,
    confidence            double precision CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    verification          text NOT NULL DEFAULT 'unverified' CHECK (verification IN ('unverified', 'verified', 'disputed')),
    producer_operation_id uuid NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    CHECK (
        (value_source = 'estimated_from_plot' AND estimate_low IS NOT NULL AND estimate_high IS NOT NULL AND estimate_low <= estimate_high)
        OR (value_source IS DISTINCT FROM 'estimated_from_plot' AND estimate_low IS NULL AND estimate_high IS NULL)
    ),
    FOREIGN KEY (project_id, visual_object_id) REFERENCES visual_objects(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, data_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, producer_operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_visual_observations_project_object ON visual_observations (project_id, visual_object_id);

CREATE TRIGGER visual_observations_append_only
    BEFORE UPDATE OR DELETE ON visual_observations
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE evidence_relations (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    from_evidence_id      uuid NOT NULL,
    to_evidence_id        uuid NOT NULL,
    relation_type         text NOT NULL CHECK (relation_type IN (
        'continues',
        'defines',
        'refers_to',
        'same_entity',
        'similar_but_different',
        'table_continuation',
        'caption_of',
        'explains',
        'qualifies',
        'depicts',
        'derived_from',
        'contradicts'
    )),
    resource_id           uuid NOT NULL,
    validation            text NOT NULL DEFAULT 'unvalidated' CHECK (validation IN ('unvalidated', 'validated', 'rejected')),
    supporting_regions    jsonb NOT NULL DEFAULT '[]'::jsonb,
    contradicting_regions jsonb NOT NULL DEFAULT '[]'::jsonb,
    operation_id          uuid,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    CHECK (from_evidence_id <> to_evidence_id),
    FOREIGN KEY (project_id, from_evidence_id) REFERENCES evidence_units(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, to_evidence_id) REFERENCES evidence_units(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, resource_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_evidence_relations_from ON evidence_relations (project_id, from_evidence_id);
CREATE INDEX idx_evidence_relations_to ON evidence_relations (project_id, to_evidence_id);

CREATE FUNCTION evidence_relations_same_document_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_from_doc_id uuid;
    v_to_doc_id   uuid;
BEGIN
    SELECT eu.document_version_id INTO v_from_doc_id
    FROM evidence_units eu
    WHERE eu.project_id = NEW.project_id AND eu.id = NEW.from_evidence_id;

    SELECT eu.document_version_id INTO v_to_doc_id
    FROM evidence_units eu
    WHERE eu.project_id = NEW.project_id AND eu.id = NEW.to_evidence_id;

    IF v_from_doc_id IS DISTINCT FROM v_to_doc_id THEN
        RAISE EXCEPTION 'evidence_relations endpoints must belong to the same document_version_id (% vs %)',
            v_from_doc_id, v_to_doc_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_relations_same_document_check
    AFTER INSERT OR UPDATE ON evidence_relations
    FOR EACH ROW EXECUTE FUNCTION evidence_relations_same_document_guard();

CREATE TABLE evidence_verifications (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id               uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    evidence_unit_id         uuid,
    visual_object_id         uuid,
    oracle                   text NOT NULL CHECK (oracle IN ('ocr', 'native_text', 'table_structure', 'vlm', 'human')),
    result                   text NOT NULL CHECK (result IN ('pass', 'fail', 'conflict')),
    reason                   text NOT NULL CHECK (length(btrim(reason)) > 0),
    actor_id                 uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    checked_at               timestamptz NOT NULL DEFAULT now(),
    source_image_artifact_id uuid,
    checked_claims           jsonb NOT NULL DEFAULT '[]'::jsonb,
    operation_id             uuid,
    UNIQUE (project_id, id),
    CHECK (num_nonnulls(evidence_unit_id, visual_object_id) = 1),
    FOREIGN KEY (project_id, evidence_unit_id) REFERENCES evidence_units(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, visual_object_id) REFERENCES visual_objects(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, source_image_artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_evidence_verifications_unit ON evidence_verifications (project_id, evidence_unit_id);
CREATE INDEX idx_evidence_verifications_visual ON evidence_verifications (project_id, visual_object_id);

CREATE TRIGGER evidence_verifications_append_only
    BEFORE UPDATE OR DELETE ON evidence_verifications
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE derivations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    layer           text NOT NULL DEFAULT 'L3' CHECK (layer = 'L3'),
    kind            text NOT NULL CHECK (kind IN ('summary', 'calculation', 'comparison', 'cross_page_relation', 'explanation')),
    formula         jsonb,
    model           text CHECK (model IS NULL OR length(btrim(model)) > 0),
    prompt_revision text CHECK (prompt_revision IS NULL OR length(btrim(prompt_revision)) > 0),
    output_claims   jsonb NOT NULL DEFAULT '[]'::jsonb,
    operation_id    uuid NOT NULL,
    status          text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'verified')),
    resource_id     uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, resource_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_derivations_project_status ON derivations (project_id, status);

CREATE TABLE derivation_inputs (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            uuid REFERENCES projects(id) ON DELETE RESTRICT,
    derivation_id         uuid NOT NULL REFERENCES derivations(id) ON DELETE RESTRICT,
    evidence_unit_id      uuid,
    visual_observation_id uuid,
    CHECK (num_nonnulls(evidence_unit_id, visual_observation_id) = 1),
    UNIQUE (derivation_id, evidence_unit_id),
    UNIQUE (derivation_id, visual_observation_id),
    FOREIGN KEY (project_id, derivation_id) REFERENCES derivations(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, evidence_unit_id) REFERENCES evidence_units(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, visual_observation_id) REFERENCES visual_observations(project_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_derivation_inputs_derivation ON derivation_inputs (derivation_id);

CREATE FUNCTION derivation_inputs_project_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.project_id IS NULL THEN
        SELECT d.project_id INTO NEW.project_id
        FROM derivations d
        WHERE d.id = NEW.derivation_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER derivation_inputs_set_project
    BEFORE INSERT OR UPDATE ON derivation_inputs
    FOR EACH ROW EXECUTE FUNCTION derivation_inputs_project_sync();

CREATE FUNCTION derivations_verified_inputs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'verified' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM derivation_inputs di
            WHERE di.derivation_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'verified derivation (%) requires at least one derivation_input', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER derivations_verified_inputs_check
    AFTER INSERT OR UPDATE ON derivations
    FOR EACH ROW EXECUTE FUNCTION derivations_verified_inputs_guard();

CREATE FUNCTION derivation_inputs_retain_verified_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM derivations d
        WHERE d.id = OLD.derivation_id
          AND d.status = 'verified'
    ) AND NOT EXISTS (
        SELECT 1
        FROM derivation_inputs di
        WHERE di.derivation_id = OLD.derivation_id
    ) THEN
        RAISE EXCEPTION 'cannot remove last input from verified derivation (%)' , OLD.derivation_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER derivation_inputs_retain_verified_check
    AFTER DELETE OR UPDATE ON derivation_inputs
    FOR EACH ROW EXECUTE FUNCTION derivation_inputs_retain_verified_guard();
