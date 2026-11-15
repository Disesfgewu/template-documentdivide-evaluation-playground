"""Real PostgreSQL tests for transactional outbox consumption (SPEC 10.2)."""
from __future__ import annotations

import threading
import time
import unittest
import uuid
from datetime import datetime
from typing import Any

import psycopg
import psycopg.errors

from lab_db.outbox import consume
from lab_db.repo import Change, record_change

from .base import FreshDatabase, h


class TestOutbox(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        self._seq = 0

    def _emit_event(
        self,
        conn: psycopg.Connection[Any],
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        topic: str,
        payload: dict[str, Any],
    ) -> None:
        self._seq += 1
        logical_id = f"res_{self._seq}_{uuid.uuid4().hex[:8]}"
        with conn.transaction():
            record_change(
                conn,
                Change(
                    project_id,
                    actor_id,
                    "create",
                    "document",
                    logical_id,
                    1,
                    h(logical_id),
                    "v1",
                    "create",
                    outbox_topic=topic,
                    outbox_payload=payload,
                ),
            )

    def test_consume_topic_filter_idempotent_and_independent_consumers(self) -> None:
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            self._emit_event(conn, project_id, actor_id, "a.created", {"seq": 1, "kind": "a"})
            self._emit_event(conn, project_id, actor_id, "b.created", {"seq": 2, "kind": "b"})
            self._emit_event(conn, project_id, actor_id, "a.created", {"seq": 3, "kind": "a"})

            received_w1: list[dict[str, Any]] = []

            def handler_w1(c: psycopg.Connection[Any], event: dict[str, Any]) -> None:
                self.assertIs(c, conn)
                self.assertEqual(
                    set(event.keys()),
                    {"event_id", "project_id", "topic", "payload", "operation_id", "created_at"},
                )
                self.assertIsInstance(event["event_id"], uuid.UUID)
                self.assertEqual(event["project_id"], project_id)
                self.assertEqual(event["topic"], "a.created")
                self.assertIsInstance(event["operation_id"], uuid.UUID)
                self.assertIsInstance(event["created_at"], datetime)
                received_w1.append(event)

            count1 = consume(conn, "worker.one", handler_w1, topics=["a.created"])
            self.assertEqual(count1, 2)
            self.assertEqual(
                [e["payload"] for e in received_w1],
                [{"seq": 1, "kind": "a"}, {"seq": 3, "kind": "a"}],
            )

            # Idempotent re-run for the same consumer returns 0 and does not invoke handler.
            count1_again = consume(conn, "worker.one", handler_w1, topics=["a.created"])
            self.assertEqual(count1_again, 0)
            self.assertEqual(len(received_w1), 2)

            # A second consumer independently receives the same 2 events.
            received_w2: list[dict[str, Any]] = []
            count2 = consume(
                conn,
                "worker_two-2",
                lambda _c, ev: received_w2.append(ev),
                topics=["a.created"],
            )
            self.assertEqual(count2, 2)
            self.assertEqual(
                [e["event_id"] for e in received_w2],
                [e["event_id"] for e in received_w1],
            )
            self.assertEqual(
                [e["payload"] for e in received_w2],
                [{"seq": 1, "kind": "a"}, {"seq": 3, "kind": "a"}],
            )
            self.assertEqual(
                consume(conn, "worker_two-2", lambda _c, ev: received_w2.append(ev), topics=["a.created"]),
                0,
            )

    def test_handler_exception_rolls_back_batch_and_redelivers(self) -> None:
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            self._emit_event(conn, project_id, actor_id, "retry.created", {"n": 1})
            self._emit_event(conn, project_id, actor_id, "retry.created", {"n": 2})

            attempted: list[dict[str, Any]] = []

            def failing_on_second(_c: psycopg.Connection[Any], event: dict[str, Any]) -> None:
                attempted.append(event)
                if len(attempted) == 2:
                    raise RuntimeError("boom on second event")

            with self.assertRaisesRegex(RuntimeError, "boom on second event"):
                consume(conn, "retry.worker", failing_on_second, topics=["retry.created"])

            self.assertEqual(len(attempted), 2)

            with conn.transaction():
                row = conn.execute(
                    "SELECT count(*) FROM outbox_consumptions WHERE consumer = %s",
                    ("retry.worker",),
                ).fetchone()
                assert row is not None
                self.assertEqual(row[0], 0)

            delivered: list[dict[str, Any]] = []
            count = consume(
                conn,
                "retry.worker",
                lambda _c, ev: delivered.append(ev),
                topics=["retry.created"],
            )
            self.assertEqual(count, 2)
            self.assertEqual([e["payload"] for e in delivered], [{"n": 1}, {"n": 2}])
            self.assertEqual(
                consume(conn, "retry.worker", lambda _c, ev: delivered.append(ev), topics=["retry.created"]),
                0,
            )

    def test_concurrent_workers_skip_locked_without_duplicates(self) -> None:
        with self.connect() as setup_conn:
            project_id, actor_id = self.make_project(setup_conn)
            for i in range(4):
                self._emit_event(setup_conn, project_id, actor_id, "parallel.created", {"idx": i})

        barrier = threading.Barrier(2, timeout=10.0)
        lock = threading.Lock()
        all_event_ids: list[uuid.UUID] = []
        per_worker_ids: list[list[uuid.UUID]] = [[], []]
        errors: list[BaseException] = []

        def worker(worker_idx: int) -> None:
            try:
                with self.connect() as worker_conn:
                    barrier.wait()
                    while True:
                        n = consume(
                            worker_conn,
                            "parallel.worker",
                            lambda _c, ev: _handle(worker_idx, ev),
                            topics=["parallel.created"],
                            batch_size=1,
                        )
                        if n == 0:
                            break
            except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                with lock:
                    errors.append(exc)

        def _handle(worker_idx: int, event: dict[str, Any]) -> None:
            time.sleep(0.5)
            with lock:
                all_event_ids.append(event["event_id"])
                per_worker_ids[worker_idx].append(event["event_id"])

        threads = [
            threading.Thread(target=worker, args=(0,), name="outbox-worker-0"),
            threading.Thread(target=worker, args=(1,), name="outbox-worker-1"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            self.assertFalse(t.is_alive(), f"Thread {t.name} timed out")

        self.assertEqual(errors, [])
        self.assertEqual(len(all_event_ids), 4)
        self.assertEqual(len(set(all_event_ids)), 4)
        self.assertGreaterEqual(len(per_worker_ids[0]), 1)
        self.assertGreaterEqual(len(per_worker_ids[1]), 1)

    def test_invalid_consumer_name_rejected(self) -> None:
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            self._emit_event(conn, project_id, actor_id, "check.created", {"ok": True})

            invalid_names = [
                "",
                "UpperCase",
                "1leading_digit",
                "_leading_underscore",
                "-leading_dash",
                ".leading_dot",
                "bad space",
                "bad/slash",
                "a" * 65,
                "valid\nnewline",
            ]
            for bad_name in invalid_names:
                with self.subTest(consumer=bad_name):
                    with self.assertRaises((ValueError, psycopg.errors.CheckViolation)):
                        consume(conn, bad_name, lambda _c, _ev: None, topics=["check.created"])

            with conn.transaction():
                row = conn.execute(
                    "SELECT event_id FROM outbox_events WHERE topic = %s LIMIT 1",
                    ("check.created",),
                ).fetchone()
                assert row is not None
                event_id = row[0]

            with self.assertRaises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO outbox_consumptions (consumer, event_id) VALUES (%s, %s)",
                        ("InvalidConsumerName", event_id),
                    )


if __name__ == "__main__":
    unittest.main()
