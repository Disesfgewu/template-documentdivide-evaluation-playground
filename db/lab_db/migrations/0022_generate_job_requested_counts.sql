-- 0022: the owner sets HOW MANY questions to generate (Issue #186).
--
-- A generation job remembers how many questions of each bucket were asked for. NULL = the plan's own targets (what every job did
-- before). The worker builds exactly that many calls (minus the questions that already exist), so asking for 10 questions sends
-- about 10 calls, not one per plan target, and the budget cap is derived from exactly those calls.
ALTER TABLE generate_jobs ADD COLUMN requested_general integer CHECK (requested_general IS NULL OR requested_general BETWEEN 0 AND 1000);
ALTER TABLE generate_jobs ADD COLUMN requested_cross_page integer CHECK (requested_cross_page IS NULL OR requested_cross_page BETWEEN 0 AND 1000);
ALTER TABLE generate_jobs ADD COLUMN requested_confusable integer CHECK (requested_confusable IS NULL OR requested_confusable BETWEEN 0 AND 1000);
ALTER TABLE generate_jobs ADD CONSTRAINT generate_jobs_requested_all_or_none CHECK (
    (requested_general IS NULL) = (requested_cross_page IS NULL) AND (requested_general IS NULL) = (requested_confusable IS NULL));
