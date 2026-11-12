-- 0015: gold answers of generated questions (P-D slice 3, PM-DECISION Issue #7 2026-11-12, Refs #7, Refs #10).
--
-- One row per (question, kind): the validated SemanticAnswer / TextualAnswer / SourceAnswer contract as jsonb, written
-- in the same transaction as the question, its facts, evidence sets and the llm_calls settlement rows.
-- * append-only (append_only_guard); a frozen benchmark takes no insert (benchmark_child_guard, as for facts);
-- * composite foreign keys keep the benchmark and the question in the same project / benchmark;
-- * gold isolation as in 0008: RLS (forced) so only roles that may read gold (owner, maintainer, reviewer via
--   lab_can_read_gold) see rows and only owner/maintainer (lab_can_write_gold) insert; participants and observers
--   see nothing.

CREATE TABLE gold_answers (
    project_id    uuid NOT NULL,
    benchmark_id  uuid NOT NULL,
    question_id   uuid NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('semantic', 'textual', 'source')),
    payload       jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 262144),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (question_id, kind),
    FOREIGN KEY (project_id, benchmark_id) REFERENCES benchmark_revisions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);
CREATE INDEX gold_answers_benchmark_idx ON gold_answers (project_id, benchmark_id);
CREATE TRIGGER gold_answers_append_only BEFORE UPDATE OR DELETE ON gold_answers
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
CREATE TRIGGER gold_answers_frozen BEFORE INSERT ON gold_answers
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();

ALTER TABLE gold_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE gold_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY gold_answers_select_gold ON gold_answers
    FOR SELECT USING (lab_can_read_gold(project_id));
CREATE POLICY gold_answers_insert_gold ON gold_answers
    FOR INSERT WITH CHECK (lab_can_write_gold(project_id));
