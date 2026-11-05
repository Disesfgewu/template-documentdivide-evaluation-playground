"""Transactional outbox consumer with per-consumer idempotent deduplication (SPEC 10.2)."""
from __future__ import annotations

import re
import uuid
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
  AND NOT (e.event_id = ANY(%s::uuid[]))
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

_CLAIM_CONSUMPTION_SQL = """
INSERT INTO outbox_consumptions (consumer, event_id)
VALUES (%s, %s)
ON CONFLICT (consumer, event_id) DO NOTHING
RETURNING event_id
"""


def consume(
    conn: psycopg.Connection[Any],
    consumer: str,
    handler: Callable[[psycopg.Connection[Any], dict[str, Any]], None],
    topics: list[str] | Sequence[str],
    batch_size: int = 50,
    max_attempts: int = 5,
    skip_event_ids: set[uuid.UUID] | None = None,
) -> int:
    """Consume up to ``batch_size`` unconsumed outbox events for ``consumer``.

    In a single transaction:
    1. Selects events whose ``topic`` is in ``topics``, whose ``event_id`` is not
       in ``skip_event_ids``, and that have not yet been consumed by ``consumer``,
       ordered by ``outbox_events.id``, locked with ``FOR UPDATE OF e SKIP LOCKED``.
    2. For each selected event, claims consumption via
       ``INSERT INTO outbox_consumptions ... ON CONFLICT DO NOTHING RETURNING event_id``
       before invoking ``handler``. Only events whose insert returns a row are
       passed to ``handler(conn, event_dict)``; events already claimed by a
       concurrent worker for the same ``consumer`` are skipped without failing
       or rolling back the batch.
    3. ``event_dict`` contains ``event_id``, ``project_id``, ``topic``,
       ``payload``, ``operation_id``, and ``created_at``.

    Locks are taken on the event rows, so two *different* consumers working
    at the same moment may skip each other's locked events; the skipped
    consumer picks them up on its next call (still at-least-once).

    If ``handler`` raises an exception, the entire transaction rolls back so no
    events in the batch are marked consumed, and the exception propagates.

    Note on poison messages (毒訊息):
        Because failed handler invocations roll back the transaction (including
        the consumption record), a poison message will block subsequent progress
        on its topic. Callers must isolate poison messages by topic or by
        polling with ``batch_size=1`` plus caller-side exception tracking
        (up to ``max_attempts``), and passing known poison event UUIDs via
        ``skip_event_ids`` to exclude them from selection.

    Returns the number of events processed (handler invoked) in this batch.
    """
    if not isinstance(consumer, str) or not _CONSUMER_RE.fullmatch(consumer):
        raise ValueError(
            f"invalid consumer name {consumer!r}; must match {CONSUMER_PATTERN}"
        )
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    if max_attempts <= 0:
        raise ValueError(f"max_attempts must be positive, got {max_attempts}")

    topic_list = list(topics)
    if not topic_list:
        return 0

    skip_list = list(skip_event_ids) if skip_event_ids else []

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_BATCH_SQL,
                (topic_list, skip_list, consumer, batch_size),
            )
            events = cur.fetchall()
            processed = 0
            for event in events:
                cur.execute(_CLAIM_CONSUMPTION_SQL, (consumer, event["event_id"]))
                if cur.fetchone() is None:
                    continue
                handler(conn, dict(event))
                processed += 1
            return processed
