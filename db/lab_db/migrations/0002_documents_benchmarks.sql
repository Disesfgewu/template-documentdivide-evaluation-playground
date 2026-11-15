-- 0002: documents/pages/evidence and four-part benchmark storage with a
-- guarded state machine (SPEC 4.1, 5, 10, 16.2, 16.6). Typed rows reference
-- resource_versions with project-consistent composite FKs.

CREATE TABLE document_versions (
    id               uuid PRIMARY KEY,
    project_id       uuid NOT NULL,
    resource_id      uuid NOT NULL UNIQUE,
    artifact_id      uuid NOT NULL,
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    page_count       integer NOT NULL CHECK (page_count >= 1),
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, resource_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT
);

CREATE TABLE pages (
    document_version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    page_index          integer NOT NULL CHECK (page_index >= 0),
    page_label          text CHECK (length(page_label) <= 64),
    rotation            integer NOT NULL CHECK (rotation IN (0, 90, 180, 270)),
    width_pt            numeric NOT NULL CHECK (width_pt > 0),
    height_pt           numeric NOT NULL CHECK (height_pt > 0),
    PRIMARY KEY (document_version_id, page_index)
);

-- page_index must be < page_count of its document.
CREATE FUNCTION pages_in_range() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.page_index >= (SELECT page_count FROM document_versions WHERE id = NEW.document_version_id) THEN
        RAISE EXCEPTION 'page_index % out of range', NEW.page_index USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER pages_in_range BEFORE INSERT OR UPDATE ON pages FOR EACH ROW EXECUTE FUNCTION pages_in_range();

CREATE TABLE evidence_units (
    id                  uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    document_version_id uuid NOT NULL,
    evidence_key        text NOT NULL CHECK (length(evidence_key) BETWEEN 1 AND 128),
    raw_content         text NOT NULL,
    normalized_content  text NOT NULL,
    extractor_revision  text NOT NULL CHECK (length(extractor_revision) BETWEEN 1 AND 128),
    ocr_confidence      double precision CHECK (ocr_confidence BETWEEN 0 AND 1),
    UNIQUE (document_version_id, evidence_key),
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT
);

CREATE TABLE evidence_regions (
    evidence_unit_id    uuid NOT NULL REFERENCES evidence_units(id) ON DELETE RESTRICT,
    ordinal             integer NOT NULL CHECK (ordinal >= 0),
    document_version_id uuid NOT NULL,
    page_index          integer NOT NULL,
    x0 double precision NOT NULL, y0 double precision NOT NULL,
    x1 double precision NOT NULL, y1 double precision NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('text', 'table', 'figure', 'image', 'formula', 'page')),
    quote               text,
    PRIMARY KEY (evidence_unit_id, ordinal),
    FOREIGN KEY (document_version_id, page_index) REFERENCES pages(document_version_id, page_index) ON DELETE RESTRICT,
    CHECK (0 <= x0 AND x0 < x1 AND x1 <= 1 AND 0 <= y0 AND y0 < y1 AND y1 <= 1),
    CHECK (kind <> 'page' OR (x0 = 0 AND y0 = 0 AND x1 = 1 AND y1 = 1))
);

-- Benchmark revision state machine (SPEC 10).
CREATE TYPE benchmark_status AS ENUM (
    'draft', 'parsing', 'planning', 'generating', 'validating', 'ready_to_freeze', 'frozen',
    'needs_review', 'paused_budget', 'failed', 'cancelled');

CREATE TABLE benchmark_transitions_allowed (
    from_status benchmark_status NOT NULL,
    to_status   benchmark_status NOT NULL,
    PRIMARY KEY (from_status, to_status)
);
INSERT INTO benchmark_transitions_allowed VALUES
    ('draft', 'parsing'), ('parsing', 'planning'), ('planning', 'generating'),
    ('generating', 'validating'), ('validating', 'ready_to_freeze'), ('ready_to_freeze', 'frozen'),
    -- branch states may be entered from any active state and resumed to where work can continue
    ('parsing', 'needs_review'), ('planning', 'needs_review'), ('generating', 'needs_review'),
    ('validating', 'needs_review'), ('ready_to_freeze', 'needs_review'),
    ('needs_review', 'planning'), ('needs_review', 'generating'), ('needs_review', 'validating'),
    ('parsing', 'paused_budget'), ('planning', 'paused_budget'), ('generating', 'paused_budget'),
    ('validating', 'paused_budget'),
    ('paused_budget', 'parsing'), ('paused_budget', 'planning'), ('paused_budget', 'generating'),
    ('paused_budget', 'validating'),
    ('draft', 'cancelled'), ('parsing', 'cancelled'), ('planning', 'cancelled'), ('generating', 'cancelled'),
    ('validating', 'cancelled'), ('ready_to_freeze', 'cancelled'), ('needs_review', 'cancelled'),
    ('paused_budget', 'cancelled'),
    ('parsing', 'failed'), ('planning', 'failed'), ('generating', 'failed'), ('validating', 'failed');

CREATE TABLE benchmark_revisions (
    id                  uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    resource_id         uuid NOT NULL UNIQUE,
    document_version_id uuid NOT NULL,
    status              benchmark_status NOT NULL DEFAULT 'draft',
    row_version         integer NOT NULL DEFAULT 1,
    compliance          text CHECK (compliance IN ('compliant', 'partial')),
    n_target            integer NOT NULL CHECK (n_target >= 20),
    q_min               integer NOT NULL CHECK (q_min >= 2),
    frozen_at           timestamptz,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, resource_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT,
    CHECK ((status = 'frozen') = (frozen_at IS NOT NULL)),
    CHECK (status <> 'frozen' OR compliance IS NOT NULL)
);

CREATE TABLE benchmark_transitions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    benchmark_id    uuid NOT NULL REFERENCES benchmark_revisions(id) ON DELETE RESTRICT,
    from_status     benchmark_status NOT NULL,
    to_status       benchmark_status NOT NULL,
    row_version     integer NOT NULL,
    reason          text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    actor_id        uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER benchmark_transitions_append_only BEFORE UPDATE OR DELETE ON benchmark_transitions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE FUNCTION benchmark_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark revisions are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'frozen' THEN
        RAISE EXCEPTION 'benchmark revision % is frozen', OLD.id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.project_id, NEW.resource_id, NEW.document_version_id, NEW.n_target, NEW.q_min)
       IS DISTINCT FROM (OLD.id, OLD.project_id, OLD.resource_id, OLD.document_version_id, OLD.n_target, OLD.q_min) THEN
        RAISE EXCEPTION 'benchmark identity/targets are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (SELECT 1 FROM benchmark_transitions_allowed
                       WHERE from_status = OLD.status AND to_status = NEW.status) THEN
            RAISE EXCEPTION 'illegal benchmark transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version must increase by exactly 1' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER benchmark_revisions_guard BEFORE UPDATE OR DELETE ON benchmark_revisions
    FOR EACH ROW EXECUTE FUNCTION benchmark_revisions_guard();

-- Four-part content. Each row belongs to one benchmark revision; once that
-- revision is frozen no child row may be inserted, changed or deleted.
CREATE TABLE questions (
    id              uuid PRIMARY KEY,
    benchmark_id    uuid NOT NULL REFERENCES benchmark_revisions(id) ON DELETE RESTRICT,
    question_key    text NOT NULL CHECK (length(question_key) BETWEEN 1 AND 128),
    ordinal         integer NOT NULL CHECK (ordinal >= 0),
    question_text   text NOT NULL CHECK (length(question_text) BETWEEN 1 AND 8000),
    question_hash   text NOT NULL CHECK (question_hash ~ '^[0-9a-f]{64}$'),
    quota_bucket    text NOT NULL CHECK (quota_bucket IN ('general', 'cross_page', 'chart_table', 'confusable')),
    payload         jsonb NOT NULL,   -- full validated Question contract
    UNIQUE (benchmark_id, question_key),
    UNIQUE (benchmark_id, ordinal),
    UNIQUE (benchmark_id, question_hash),
    UNIQUE (benchmark_id, id)
);

CREATE TABLE facts (
    id              uuid PRIMARY KEY,
    benchmark_id    uuid NOT NULL,
    question_id     uuid NOT NULL,
    fact_key        text NOT NULL CHECK (length(fact_key) BETWEEN 1 AND 128),
    statement       text NOT NULL CHECK (length(statement) >= 1),
    weight          double precision NOT NULL CHECK (weight > 0 AND weight < 'Infinity'),
    partial_credit_rule text,          -- null = no 0.5 score allowed
    UNIQUE (question_id, fact_key),
    UNIQUE (question_id, id),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);

CREATE TABLE evidence_sets (
    id              uuid PRIMARY KEY,
    benchmark_id    uuid NOT NULL,
    question_id     uuid NOT NULL,
    set_key         text NOT NULL CHECK (length(set_key) BETWEEN 1 AND 128),
    UNIQUE (question_id, set_key),
    UNIQUE (benchmark_id, id),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);

CREATE TABLE evidence_set_members (
    evidence_set_id   uuid NOT NULL,
    benchmark_id      uuid NOT NULL,
    evidence_unit_id  uuid NOT NULL REFERENCES evidence_units(id) ON DELETE RESTRICT,
    PRIMARY KEY (evidence_set_id, evidence_unit_id),   -- non-repeating members
    FOREIGN KEY (benchmark_id, evidence_set_id) REFERENCES evidence_sets(benchmark_id, id) ON DELETE RESTRICT
);

CREATE TABLE distractors (
    question_id       uuid NOT NULL,
    benchmark_id      uuid NOT NULL,
    evidence_unit_id  uuid NOT NULL REFERENCES evidence_units(id) ON DELETE RESTRICT,
    confusion_reason  text NOT NULL CHECK (length(confusion_reason) >= 1),
    disambiguating_condition text NOT NULL CHECK (length(disambiguating_condition) >= 1),
    PRIMARY KEY (question_id, evidence_unit_id),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);

-- Evidence used by a benchmark must come from that benchmark's document.
CREATE FUNCTION evidence_from_benchmark_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT document_version_id FROM evidence_units WHERE id = NEW.evidence_unit_id)
       IS DISTINCT FROM (SELECT document_version_id FROM benchmark_revisions WHERE id = NEW.benchmark_id) THEN
        RAISE EXCEPTION 'evidence % is not from the benchmark document', NEW.evidence_unit_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER evidence_set_members_document BEFORE INSERT OR UPDATE ON evidence_set_members
    FOR EACH ROW EXECUTE FUNCTION evidence_from_benchmark_document();
CREATE TRIGGER distractors_document BEFORE INSERT OR UPDATE ON distractors
    FOR EACH ROW EXECUTE FUNCTION evidence_from_benchmark_document();

-- Children of a frozen benchmark are immutable.
CREATE FUNCTION benchmark_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b uuid;
BEGIN
    b := CASE WHEN TG_OP = 'DELETE' THEN OLD.benchmark_id ELSE NEW.benchmark_id END;
    IF (SELECT status FROM benchmark_revisions WHERE id = b) = 'frozen'
       OR (TG_OP = 'UPDATE' AND (SELECT status FROM benchmark_revisions WHERE id = OLD.benchmark_id) = 'frozen') THEN
        RAISE EXCEPTION '% of frozen benchmark % cannot change', TG_TABLE_NAME, b
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER questions_frozen BEFORE INSERT OR UPDATE OR DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER facts_frozen BEFORE INSERT OR UPDATE OR DELETE ON facts
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER evidence_sets_frozen BEFORE INSERT OR UPDATE OR DELETE ON evidence_sets
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER evidence_set_members_frozen BEFORE INSERT OR UPDATE OR DELETE ON evidence_set_members
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER distractors_frozen BEFORE INSERT OR UPDATE OR DELETE ON distractors
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
