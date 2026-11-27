-- 0029: a REVIEWED correctness per question of a run, next to the judge's value (Issue #218, PM-DECISION: item 4 first, as its own small PR).
-- The judge value in question_scores is never touched. A review is one more row here: append-only, the latest row per (run, question, metric) wins,
-- earlier rows stay as history. reviewer_kind says WHO judged: 'human' (the owner) or 'model_assisted' (a model wrote the verdict); the web never shows
-- the second as the first. Written only by the owner or a maintainer (the API decides; the guard below also checks it for the acting user).
CREATE TABLE run_score_reviews (
    id             uuid PRIMARY KEY,
    seq            bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    project_id     uuid NOT NULL,
    run_id         uuid NOT NULL,
    benchmark_id   uuid NOT NULL,
    question_id    uuid NOT NULL,
    metric         text NOT NULL CHECK (metric = 'correctness'),
    value          double precision NOT NULL CHECK (value >= 0 AND value <= 100),
    reviewer_kind  text NOT NULL CHECK (reviewer_kind IN ('human', 'model_assisted')),
    reviewer_label text NOT NULL CHECK (length(btrim(reviewer_label)) BETWEEN 1 AND 128),
    note           text CHECK (note IS NULL OR length(note) <= 500),
    actor_id       uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, run_id) REFERENCES runs(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, benchmark_id) REFERENCES runs(id, benchmark_id) ON DELETE RESTRICT,
    FOREIGN KEY (benchmark_id, question_id) REFERENCES questions(benchmark_id, id) ON DELETE RESTRICT
);
CREATE INDEX run_score_reviews_latest ON run_score_reviews (run_id, question_id, metric, seq DESC);
CREATE TRIGGER run_score_reviews_append_only BEFORE UPDATE OR DELETE ON run_score_reviews
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

CREATE FUNCTION run_score_reviews_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM project_members WHERE project_id = NEW.project_id AND actor_id = NEW.actor_id AND role IN ('owner', 'maintainer')) THEN
        RAISE EXCEPTION 'only a project owner or maintainer may record a reviewed score' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER run_score_reviews_guard BEFORE INSERT ON run_score_reviews
    FOR EACH ROW EXECUTE FUNCTION run_score_reviews_guard();
