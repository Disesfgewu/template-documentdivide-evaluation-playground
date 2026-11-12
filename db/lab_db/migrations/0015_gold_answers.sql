-- 0015: the full gold of a question (P-D slice 3, Refs #7, Refs #10; PM-DECISION 2026-11-12).
--
-- questions.payload holds the Question contract only, facts/evidence_sets hold the queryable projection. The rest of the
-- gold (SemanticAnswer: reference answer, conditions, calculation, allowed equivalences, scoring rules; TextualAnswer:
-- checks; SourceAnswer: evidence sets, fact support, distractors) lives here, one validated contract per (question, kind).
-- Append-only, size-capped, frozen with the benchmark, and under the same row level security as facts (0008): only
-- owner/maintainer/reviewer read it, only owner/maintainer write it; participants and observers see nothing.

CREATE TABLE gold_answers (
    id            uuid PRIMARY KEY,
    benchmark_id  uuid NOT NULL,
    question_id   uuid NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('semantic', 'textual', 'source')),
    payload       jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 262144),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question_id, kind),
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);
CREATE INDEX gold_answers_benchmark_idx ON gold_answers (benchmark_id);
CREATE TRIGGER gold_answers_frozen BEFORE INSERT OR UPDATE OR DELETE ON gold_answers
    FOR EACH ROW EXECUTE FUNCTION benchmark_child_guard();
CREATE TRIGGER gold_answers_append_only BEFORE UPDATE OR DELETE ON gold_answers
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

ALTER TABLE gold_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE gold_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY gold_answers_select_gold ON gold_answers
    FOR SELECT
    USING (lab_can_read_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = gold_answers.benchmark_id)));
CREATE POLICY gold_answers_insert_gold ON gold_answers
    FOR INSERT
    WITH CHECK (lab_can_write_gold((SELECT project_id FROM benchmark_revisions b WHERE b.id = gold_answers.benchmark_id)));
