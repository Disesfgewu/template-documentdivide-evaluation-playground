-- 0023: a method developer's answers, uploaded as a file (Issue #189, SPEC 9.2 / 16.5 participant flow, Refs #11 #12 #13).
--
-- The owner gives the frozen QUESTIONS (no gold) to a developer; the developer returns, per question, an answer and the source
-- passages (page + quoted text) it relied on; the system checks the quotes against the system parse and scores the file against
-- the gold. This reuses runs / attempts / submissions / retrieved_hits / citations. New here:
--   * execution_mode / transport 'offline_file' (the system never talks to that method; what it says is only declared);
--   * submissions.failure_reason 'not_submitted': a question the file did not answer, recorded, never guessed;
--   * external_submissions: one row per upload (the file's sha256 and artifact, who, method name/version/notes, the validation report);
--     a re-upload is a NEW run, nothing is overwritten;
--   * submission_source_matches: for every submitted source passage, where the quote was found on the cited page of the system parse
--     (match_quality) or that it was NOT found (text_found false): a fabricated quote is a visible fact, and the judge only ever sees
--     the system's own text of the matched units, never a quote that was not found.

ALTER TABLE method_revisions DROP CONSTRAINT method_revisions_execution_mode_check;
ALTER TABLE method_revisions ADD CONSTRAINT method_revisions_execution_mode_check CHECK (execution_mode IN ('http', 'jsonl', 'offline_file'));
ALTER TABLE attempts DROP CONSTRAINT attempts_transport_check;
ALTER TABLE attempts ADD CONSTRAINT attempts_transport_check CHECK (transport IN ('http', 'jsonl', 'offline_file'));
ALTER TABLE submissions DROP CONSTRAINT submissions_failure_reason_check;
ALTER TABLE submissions ADD CONSTRAINT submissions_failure_reason_check CHECK (
    failure_reason IS NULL OR failure_reason IN ('budget_exhausted', 'provider_error', 'invalid_model_reply', 'outcome_unknown', 'not_submitted'));

CREATE TABLE external_submissions (
    run_id          uuid PRIMARY KEY,
    project_id      uuid NOT NULL,
    benchmark_id    uuid NOT NULL,
    method_name     text NOT NULL CHECK (length(btrim(method_name)) BETWEEN 1 AND 128),
    method_version  text CHECK (method_version IS NULL OR length(method_version) <= 128),
    notes           text CHECK (notes IS NULL OR length(notes) <= 2000),
    file_sha256     text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    file_artifact_id uuid NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    uploaded_by     uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    accepted_partial boolean NOT NULL,
    report          jsonb NOT NULL CHECK (jsonb_typeof(report) = 'object' AND octet_length(report::text) <= 262144),
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, benchmark_id) REFERENCES runs(id, benchmark_id) ON DELETE RESTRICT
);
CREATE INDEX external_submissions_benchmark_idx ON external_submissions (project_id, benchmark_id, created_at DESC);
CREATE TRIGGER external_submissions_append_only BEFORE UPDATE OR DELETE ON external_submissions
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE TABLE submission_source_matches (
    submission_id   uuid NOT NULL,
    hit_key         text NOT NULL,
    cited_page      integer NOT NULL CHECK (cited_page >= 1),          -- 1-based, as the developer wrote it
    match_quality   text NOT NULL CHECK (match_quality IN ('exact_in_unit', 'exact_in_page', 'not_found', 'page_without_text')),
    text_found      boolean NOT NULL,
    quote_coverage  double precision NOT NULL CHECK (quote_coverage BETWEEN 0 AND 1),   -- share of the quote's content words that are on the page
    -- the text of the matched unit(s) FROM THE SYSTEM PARSE (what the judge may see); NULL exactly when the quote was not found. The
    -- developer's own string is never evidence: for a quote that is not on the cited page the judge sees nothing of it.
    matched_text    text CHECK (matched_text IS NULL OR length(matched_text) BETWEEN 1 AND 8000),
    CHECK (text_found = (matched_text IS NOT NULL)),
    PRIMARY KEY (submission_id, hit_key),
    FOREIGN KEY (submission_id, hit_key) REFERENCES retrieved_hits(submission_id, hit_key) ON DELETE RESTRICT,
    CHECK (text_found = (match_quality IN ('exact_in_unit', 'exact_in_page')))
);
CREATE TRIGGER submission_source_matches_append_only BEFORE UPDATE OR DELETE ON submission_source_matches
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
