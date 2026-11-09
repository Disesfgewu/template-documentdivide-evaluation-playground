-- 0012: the stored outcome of a parse job (PM plan item 5, Refs #7; PM approved this number on Issue #7).
--
-- One parse_results row per parse job holds the validated manifest, capability manifest and
-- ParseResult as JSON, plus the committed artifacts that hold the raw parser output (so every
-- raw_result_ref in the result points at stored bytes). parse_page_results is the per-page view the
-- API reads: the page status and the evidence units and observations that belong to that page.
-- Both are append-only and written in the same transaction that marks the job succeeded
-- (lab_db.jobs.finish hook); a changed result is a new job, never an update.

CREATE TABLE parse_results (
    job_id              uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    document_version_id uuid NOT NULL,
    operation_id        uuid NOT NULL,
    manifest            jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    capabilities        jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'object'),
    result              jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    raw_artifact_ids    uuid[] NOT NULL CHECK (cardinality(raw_artifact_ids) >= 1),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, operation_id),
    UNIQUE (project_id, job_id),
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES parse_jobs(job_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX parse_results_document_idx ON parse_results (project_id, document_version_id, created_at);

CREATE TABLE parse_page_results (
    job_id              uuid NOT NULL,
    project_id          uuid NOT NULL,
    document_version_id uuid NOT NULL,
    page_index          integer NOT NULL CHECK (page_index >= 0),
    status              text NOT NULL CHECK (status IN ('ok', 'partial', 'needs_review', 'failed')),
    diagnostics         jsonb NOT NULL CHECK (jsonb_typeof(diagnostics) = 'array'),
    evidence            jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'),
    observations        jsonb NOT NULL CHECK (jsonb_typeof(observations) = 'array'),
    PRIMARY KEY (job_id, page_index),
    FOREIGN KEY (project_id, job_id) REFERENCES parse_results(project_id, job_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX parse_page_results_document_idx ON parse_page_results (project_id, document_version_id, page_index);

-- A page row must belong to the same document version as its result, and lie inside the document.
CREATE FUNCTION parse_page_results_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pages integer;
    owner uuid;
BEGIN
    SELECT document_version_id INTO owner FROM parse_results WHERE job_id = NEW.job_id;
    IF owner IS DISTINCT FROM NEW.document_version_id THEN
        RAISE EXCEPTION 'page result belongs to another document version than its parse result'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT page_count INTO pages FROM document_versions
        WHERE project_id = NEW.project_id AND id = NEW.document_version_id;
    IF NEW.page_index >= pages THEN
        RAISE EXCEPTION 'page index % is outside the document (% pages)', NEW.page_index, pages
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER parse_page_results_pages BEFORE INSERT ON parse_page_results
    FOR EACH ROW EXECUTE FUNCTION parse_page_results_check();

-- The result's document version must be the one its job parsed.
CREATE FUNCTION parse_results_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected uuid;
    expected_op uuid;
BEGIN
    SELECT document_version_id, operation_id INTO expected, expected_op FROM parse_jobs WHERE job_id = NEW.job_id;
    IF expected IS DISTINCT FROM NEW.document_version_id OR expected_op IS DISTINCT FROM NEW.operation_id THEN
        RAISE EXCEPTION 'parse result does not match the document version and operation of its job'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER parse_results_match_job BEFORE INSERT ON parse_results
    FOR EACH ROW EXECUTE FUNCTION parse_results_check();

CREATE TRIGGER parse_results_append_only BEFORE UPDATE OR DELETE ON parse_results
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
CREATE TRIGGER parse_page_results_append_only BEFORE UPDATE OR DELETE ON parse_page_results
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
