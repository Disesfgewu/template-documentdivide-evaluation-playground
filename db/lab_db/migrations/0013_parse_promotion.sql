-- 0013: promote a stored parse result into question-citable evidence (PM-DECISION P-A, Refs #7, Refs #10).
--
-- D1 (owner-accepted): a benchmark revision pins exactly ONE parse job (benchmark_parse_sources, append-only;
-- the pin is written once, never moved: re-parsing produces a new benchmark revision).
-- D3 (owner-confirmed): evidence_units/evidence_regions are written lazily, when a benchmark is built from the
-- parse result (lab_db.promote.promote_parse_result), not when the parse finishes.
-- G8 hard rule: only clean pages are promoted; the pin row records what was promoted and what was skipped and why,
-- and the residual limit of the hidden-text protection, so it travels with the benchmark's provenance.

-- Where a promoted evidence unit came from: one row per (unit, parse job) that produced it (a unit is stored once and
-- reused by later promotions, each of which records its own job).
CREATE TABLE evidence_unit_sources (
    evidence_unit_id     uuid NOT NULL REFERENCES evidence_units(id) ON DELETE RESTRICT,
    project_id           uuid NOT NULL,
    parse_job_id         uuid NOT NULL,
    source_evidence_id   text NOT NULL CHECK (length(source_evidence_id) BETWEEN 1 AND 128),
    page_index           integer NOT NULL CHECK (page_index >= 0),
    FOREIGN KEY (project_id, parse_job_id) REFERENCES parse_results(project_id, job_id) ON DELETE RESTRICT,
    PRIMARY KEY (evidence_unit_id, parse_job_id),
    UNIQUE (parse_job_id, source_evidence_id)
);
CREATE TRIGGER evidence_unit_sources_append_only BEFORE UPDATE OR DELETE ON evidence_unit_sources
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

-- The pin: benchmark revision -> parse job, with the promotion provenance.
CREATE TABLE benchmark_parse_sources (
    benchmark_id         uuid PRIMARY KEY REFERENCES benchmark_revisions(id) ON DELETE RESTRICT,
    project_id           uuid NOT NULL,
    parse_job_id         uuid NOT NULL,
    document_version_id  uuid NOT NULL,
    rule                 text NOT NULL CHECK (length(rule) BETWEEN 1 AND 64),
    -- Counts and per-page reasons; the residual limit text; never document content.
    provenance           jsonb NOT NULL CHECK (jsonb_typeof(provenance) = 'object' AND octet_length(provenance::text) <= 1048576),
    promoted_by          uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    promoted_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, parse_job_id) REFERENCES parse_results(project_id, job_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX benchmark_parse_sources_job_idx ON benchmark_parse_sources (project_id, parse_job_id);
CREATE TRIGGER benchmark_parse_sources_append_only BEFORE UPDATE OR DELETE ON benchmark_parse_sources
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

-- The pinned parse job must be a parse of the benchmark's own document version, and a frozen benchmark takes no pin.
CREATE FUNCTION benchmark_parse_sources_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    bench_doc uuid;
    bench_status benchmark_status;
    result_doc uuid;
BEGIN
    SELECT document_version_id, status INTO bench_doc, bench_status FROM benchmark_revisions WHERE id = NEW.benchmark_id;
    SELECT document_version_id INTO result_doc FROM parse_results WHERE job_id = NEW.parse_job_id;
    IF bench_doc IS DISTINCT FROM NEW.document_version_id OR result_doc IS DISTINCT FROM NEW.document_version_id THEN
        RAISE EXCEPTION 'the parse job is not a parse of the benchmark document version'
            USING ERRCODE = 'check_violation';
    END IF;
    IF bench_status IN ('frozen', 'cancelled', 'failed') THEN
        RAISE EXCEPTION 'benchmark is % and takes no parse pin', bench_status USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER benchmark_parse_sources_check BEFORE INSERT ON benchmark_parse_sources
    FOR EACH ROW EXECUTE FUNCTION benchmark_parse_sources_check();
