-- 0025: per-page parse staging (Issue #205 / #204 PM-DECISION GO on the incremental-persistence design).
--
-- A parse result used to be written in ONE transaction after the whole document was parsed (the record of an 834-page manual is 70 MB). Now every page is
-- stored in its own short transaction as soon as it is final, in a STAGING area that no reader queries; the final tables (parse_results, parse_page_results,
-- append-only) are filled only inside the job's atomic "succeeded" transition by a server-side copy from staging, which is then deleted. A running, failed
-- or cancelled job therefore has no row in any table the parse-results API, promote/plan, the benchmark build or question generation read: unusable BY
-- CONSTRUCTION. Staging is mutable and deletable on purpose (no append-only trigger).
--
-- It also replaces 0024's parse_progress: pages_done is count(parse_staging_pages).
DROP TABLE IF EXISTS parse_progress;

CREATE TABLE parse_staging (
    job_id     uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    identity   jsonb NOT NULL CHECK (jsonb_typeof(identity) = 'object' AND octet_length(identity::text) <= 4096),
    created_at timestamptz NOT NULL DEFAULT now(),
    touched_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE CASCADE
);

CREATE TABLE parse_staging_pages (
    job_id      uuid NOT NULL REFERENCES parse_staging(job_id) ON DELETE CASCADE,
    page_index  integer NOT NULL CHECK (page_index >= 0),
    status      text NOT NULL CHECK (status IN ('ok', 'partial', 'needs_review', 'failed')),
    diagnostics jsonb NOT NULL CHECK (jsonb_typeof(diagnostics) = 'array' AND octet_length(diagnostics::text) <= 65536),
    evidence    jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND octet_length(evidence::text) <= 16777216),
    observations jsonb NOT NULL CHECK (jsonb_typeof(observations) = 'array' AND octet_length(observations::text) <= 16777216),
    run_key     text CHECK (run_key IS NULL OR length(run_key) BETWEEN 1 AND 128),     -- the batch run whose output this page came from
    page_sha256 text NOT NULL CHECK (page_sha256 ~ '^[0-9a-f]{64}$'),
    page_bytes  integer NOT NULL CHECK (page_bytes >= 0),
    PRIMARY KEY (job_id, page_index)
);

-- One row per batch run: its small manifest and the raw upstream output bytes. The raw bytes stay HERE until the job succeeds (then they are committed as
-- artifacts); a failed job leaves no committed artifact behind.
CREATE TABLE parse_staging_runs (
    job_id   uuid NOT NULL REFERENCES parse_staging(job_id) ON DELETE CASCADE,
    run_key  text NOT NULL CHECK (length(run_key) BETWEEN 1 AND 128),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object' AND octet_length(manifest::text) <= 1048576),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'object' AND octet_length(capabilities::text) <= 1048576),
    raw_result_refs jsonb NOT NULL CHECK (jsonb_typeof(raw_result_refs) = 'array'),
    raw         bytea[] NOT NULL,
    PRIMARY KEY (job_id, run_key)
);
CREATE INDEX parse_staging_touched_idx ON parse_staging (touched_at);
