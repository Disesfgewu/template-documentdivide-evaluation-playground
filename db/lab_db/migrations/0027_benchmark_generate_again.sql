-- 0027: a benchmark can be generated again (Issue #215, PM).
--   generating -> planning   a generation job that failed before it wrote any question gives the benchmark back, so generate can be started again
--   validating -> generating a top-up: the owner asked for N questions and fewer were written, so a second generation adds the missing ones
-- Nothing else about the state machine changes: freezing still only goes validating -> ready_to_freeze -> frozen.
INSERT INTO benchmark_transitions_allowed (from_status, to_status) VALUES ('generating', 'planning'), ('validating', 'generating')
ON CONFLICT DO NOTHING;
