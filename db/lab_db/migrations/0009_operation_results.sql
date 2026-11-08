-- 0009: the original response of a finished operation (SPEC 10.1, 16.4).
--
-- An idempotent retry of a request that was rejected must get the response
-- the first attempt got, not a response recomputed from today's parser,
-- limits or code. The response is saved in the same transaction as the
-- rejection's audit event and never changes afterwards.

CREATE TABLE operation_results (
    project_id    uuid NOT NULL,
    operation_id  uuid NOT NULL,
    http_status   smallint NOT NULL CHECK (http_status BETWEEN 400 AND 599),
    response_body jsonb NOT NULL CHECK (jsonb_typeof(response_body) = 'object'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, operation_id),
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE TRIGGER operation_results_append_only BEFORE UPDATE OR DELETE ON operation_results
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
