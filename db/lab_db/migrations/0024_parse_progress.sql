-- 0024: how far a running parse job is (Issue #205, Refs #13).
--
-- A parse of a long document (834 pages took about 7 minutes) showed only "running". The parse worker now writes one row per job: pages
-- whose first attempt is over / pages in the document, updated after every batch. It is a progress hint, not a result: nothing reads it for
-- a decision, a restarted job overwrites it, and the job's status stays the only truth. Mutable on purpose (unlike the append-only parse
-- results); rows are small (one per parse job).
CREATE TABLE parse_progress (
    job_id      uuid PRIMARY KEY,
    project_id  uuid NOT NULL,
    pages_done  integer NOT NULL CHECK (pages_done >= 0),
    pages_total integer NOT NULL CHECK (pages_total >= 1),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (pages_done <= pages_total),
    FOREIGN KEY (project_id, job_id) REFERENCES jobs(project_id, id) ON DELETE RESTRICT
);
