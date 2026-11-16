-- 0026: notes recorded on a document version (Issues #204 and #203, PM-DECISION).
--
-- A benign reader note that is on the allowlist (a non-zero-indexed xref table that pypdf corrects, bytes after %%EOF) and a document without a text layer
-- (a scan) are ACCEPTED and recorded here, so the owner sees them. The stored bytes and sha256 stay the original (no silent repair). One row per code,
-- append-only.
CREATE TABLE document_version_flags (
    document_version_id uuid NOT NULL,
    project_id          uuid NOT NULL,
    code                text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_version_id, code),
    FOREIGN KEY (project_id, document_version_id) REFERENCES document_versions(project_id, id) ON DELETE RESTRICT
);
CREATE TRIGGER document_version_flags_append_only BEFORE DELETE OR UPDATE ON document_version_flags
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
