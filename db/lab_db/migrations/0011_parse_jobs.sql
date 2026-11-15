-- 0011: the input of a parse job (SPEC 8, 9.x parser request; PM plan item 2 on Issue #7).
--
-- A job row alone says nothing about what it works on. parse_jobs binds one
-- job to the document version it parses, the original page indices, the
-- profile revision and an optional budget reference. A request is the
-- operation that created it (kind 'document.parse', idempotency scope
-- 'document.parse'), so a retry with the same key finds the same job.
-- Append-only: a changed request is a new job.

CREATE TABLE parse_jobs (
    job_id              uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    document_version_id uuid NOT NULL,
    profile             text NOT NULL CHECK (profile ~ '^[a-z][a-z0-9_.:-]{0,127}$'),
    page_selection      integer[] CHECK (page_selection IS NULL
                                         OR (cardinality(page_selection) BETWEEN 1 AND 2000
                                             AND array_ndims(page_selection) = 1
                                             AND array_position(page_selection, NULL) IS NULL)),
    budget_ref          text CHECK (budget_ref IS NULL OR length(budget_ref) BETWEEN 1 AND 128),
    operation_id        uuid NOT NULL,
    requested_by        uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, operation_id),
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX parse_jobs_document_idx ON parse_jobs (project_id, document_version_id, created_at);

-- Page indices are original, 0-based, strictly ascending and inside the document.
CREATE FUNCTION parse_jobs_check_pages() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pages integer;
    i integer;
BEGIN
    SELECT page_count INTO pages FROM document_versions
        WHERE project_id = NEW.project_id AND id = NEW.document_version_id;
    IF NEW.page_selection IS NOT NULL THEN
        FOR i IN 1 .. cardinality(NEW.page_selection) LOOP
            IF NEW.page_selection[i] < 0 OR NEW.page_selection[i] >= pages THEN
                RAISE EXCEPTION 'page index % is outside the document (% pages)', NEW.page_selection[i], pages
                    USING ERRCODE = 'check_violation';
            END IF;
            IF i > 1 AND NEW.page_selection[i] <= NEW.page_selection[i - 1] THEN
                RAISE EXCEPTION 'page_selection must be strictly ascending' USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER parse_jobs_pages BEFORE INSERT ON parse_jobs
    FOR EACH ROW EXECUTE FUNCTION parse_jobs_check_pages();

CREATE TRIGGER parse_jobs_append_only BEFORE UPDATE OR DELETE ON parse_jobs
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
