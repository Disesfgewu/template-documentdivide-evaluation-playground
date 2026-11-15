-- 0021: what the question-generation worker reports about itself (Issue #181, Refs #13).
--
-- The API cannot see the generate worker's environment (it is another container, and the key must stay there). The worker
-- therefore writes a small status row every few seconds: its name, the provider host and protocol it will call. NEVER the key,
-- never a prompt or a model name it has not used. /readyz reads the row (fresh = the worker is running) so the page can say
-- what is true instead of the API's legacy provider_mode label.
CREATE TABLE worker_status (
    worker_name   text PRIMARY KEY CHECK (length(worker_name) BETWEEN 1 AND 128),
    kind          text NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    provider_host text CHECK (provider_host IS NULL OR length(provider_host) <= 255),
    protocol      text CHECK (protocol IS NULL OR length(protocol) <= 32),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX worker_status_kind_idx ON worker_status (kind, updated_at DESC);
