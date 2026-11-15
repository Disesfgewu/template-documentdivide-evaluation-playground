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
    -- Size ceilings are checked again here (persist_parse_result enforces the overall cap first).
    manifest            jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object' AND octet_length(manifest::text) <= 1048576),
    capabilities        jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'object' AND octet_length(capabilities::text) <= 1048576),
    -- The ParseResult without its per-page statuses: those live only in parse_page_results.
    result              jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object' AND NOT (result ? 'pages')
                                              AND octet_length(result::text) <= 16777216),
    -- sha256 of the canonical record (manifest, capabilities, result, every page): an idempotent
    -- replay must match all of it, not just the result.
    record_sha256       text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
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
    diagnostics         jsonb NOT NULL CHECK (jsonb_typeof(diagnostics) = 'array' AND octet_length(diagnostics::text) <= 65536),
    evidence            jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND octet_length(evidence::text) <= 16777216),
    observations        jsonb NOT NULL CHECK (jsonb_typeof(observations) = 'array' AND octet_length(observations::text) <= 16777216),
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
