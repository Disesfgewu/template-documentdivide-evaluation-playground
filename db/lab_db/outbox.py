"""Transactional outbox consumer with per-consumer idempotent deduplication (SPEC 10.2)."""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

CONSUMER_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_CONSUMER_RE = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")

_SELECT_BATCH_SQL = """
SELECT e.event_id, e.project_id, e.topic, e.payload, e.operation_id, e.created_at
FROM outbox_events e
WHERE e.topic = ANY(%s)
  AND NOT EXISTS (
      SELECT 1
      FROM outbox_consumptions c
      WHERE c.consumer = %s
        AND c.event_id = e.event_id
  )
ORDER BY e.id
LIMIT %s
FOR UPDATE OF e SKIP LOCKED
"""

_INSERT_CONSUMPTION_SQL = """
INSERT INTO outbox_consumptions (consumer, event_id)
VALUES (%s, %s)
"""


def consume(
    conn: psycopg.Connection[Any],
    consumer: str,
    handler: Callable[[psycopg.Connection[Any], dict[str, Any]], None],
    topics: list[str] | Sequence[str],
    batch_size: int = 50,
) -> int:
    """Consume up to ``batch_size`` unconsumed outbox events for ``consumer``.

    In a single transaction:
    1. Selects events whose ``topic`` is in ``topics`` and that have not yet been
       consumed by ``consumer``, ordered by ``outbox_events.id``, locked with
       ``FOR UPDATE OF e SKIP LOCKED``.
    2. Invokes ``handler(conn, event_dict)`` for each event (where ``event_dict``
       contains ``event_id``, ``project_id``, ``topic``, ``payload``,
       ``operation_id``, and ``created_at``).
    3. Records each processed event in ``outbox_consumptions``.

    Locks are taken on the event rows, so two *different* consumers working
    at the same moment may skip each other's locked events; the skipped
    consumer picks them up on its next call (still at-least-once).

    If ``handler`` raises an exception, the entire transaction rolls back so no
    events in the batch are marked consumed, and the exception propagates.
    Returns the number of events processed in this batch.
    """
    if not isinstance(consumer, str) or not _CONSUMER_RE.fullmatch(consumer):
        raise ValueError(
            f"invalid consumer name {consumer!r}; must match {CONSUMER_PATTERN}"
        )
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    topic_list = list(topics)
    if not topic_list:
        return 0

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_BATCH_SQL, (topic_list, consumer, batch_size))
            events = cur.fetchall()
            for event in events:
                handler(conn, dict(event))
                cur.execute(_INSERT_CONSUMPTION_SQL, (consumer, event["event_id"]))
            return len(events)
