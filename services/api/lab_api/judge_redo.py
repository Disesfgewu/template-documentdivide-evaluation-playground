"""Which judged questions of a run can be judged again: the ones whose latest judged score is an ERROR the judge side caused (Issue #215 item 3).

An error row is never a 0 (the owner is told so); it only means the judge's reply could not be used. Those rows, and only those, can be re-judged
with the same model and the same prompt revision. A method failure (not_submitted, the answering call failed), a missing gold or an oversized answer
would fail again for the same reason, so they are not offered."""
from __future__ import annotations

import uuid

import psycopg

# first reason code of a correctness ERROR row -> worth another try: the reply was unusable, the provider failed, or the call was never made
REDO_CODES = ("invalid_judge_reply", "judge_fact_mismatch", "judge_claim_mismatch", "provider_error", "outcome_unknown", "budget_exhausted")


def redo_state(conn: psycopg.Connection, run_id: uuid.UUID) -> dict:
    """The latest evaluation of the run and what can be redone in it: ``evaluation_id``, ``grader``, ``judged``, ``question_ids``, ``models``, ``revisions``."""
    prev = conn.execute("SELECT id, grader_revision FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()
    if prev is None:
        return {"evaluation_id": None, "grader": None, "judged": False, "question_ids": set(), "models": set(), "revisions": set()}
    ids = {q for (q,) in conn.execute(
        "SELECT question_id FROM question_scores WHERE evaluation_id = %s AND metric_name = 'correctness' AND status = 'error' "
        "AND reason_codes[1] = ANY(%s)", (prev[0], list(REDO_CODES)))}
    models, revisions = set(), set()
    for model, revision in conn.execute("SELECT DISTINCT judge_model, judge_prompt_revision FROM question_scores WHERE evaluation_id = %s "
                                        "AND judge_model IS NOT NULL", (prev[0],)):
        models.add(model)
        revisions.add(revision)
    return {"evaluation_id": prev[0], "grader": prev[1], "judged": "llm-judge" in prev[1], "question_ids": ids, "models": models, "revisions": revisions}
