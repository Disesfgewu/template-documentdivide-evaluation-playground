"""Real PostgreSQL concurrency and reliability regression tests for lab_db (outbox, artifacts, storage, jobs)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime
from pathlib import Path
import tempfile
import threading
import uuid
from unittest import mock

from psycopg import errors

from lab_db import artifacts, jobs, outbox, storage
from tests.db.base import FreshDatabase, h


class TestPyFix(FreshDatabase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _insert_operation(self, conn, project_id: uuid.UUID) -> uuid.UUID:
        """A succeeded operation owned by one of the project's members."""
        op_id = uuid.uuid4()
        actor = conn.execute("SELECT actor_id FROM project_members WHERE project_id = %s LIMIT 1",
                             (project_id,)).fetchone()[0]
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'test.op', %s, 'succeeded', now())", (op_id, project_id, actor))
        return op_id

    def _insert_outbox_event(
        self,
        conn,
        project_id: uuid.UUID,
        op_id: uuid.UUID,
        topic: str = "test.topic",
    ) -> uuid.UUID:
        ev_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO outbox_events (event_id, project_id, topic, payload, operation_id)
            VALUES (%s, %s, %s, '{"ok": true}'::jsonb, %s)
            """,
            (ev_id, project_id, topic, op_id),
        )
        return ev_id

    # ------------------------------------------------------------------
    # 1. outbox.consume tests
    # ------------------------------------------------------------------
    def test_outbox_concurrent_same_consumer_deduplicates_without_error(self):
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            with conn.transaction():
                op_id = self._insert_operation(conn, project_id)
                event_ids = [
                    self._insert_outbox_event(conn, project_id, op_id, "doc.parsed")
                    for _ in range(10)
                ]

        handled: list[uuid.UUID] = []
        lock = threading.Lock()

        def worker_fn() -> int:
            def handler(_c, ev):
                with lock:
                    handled.append(ev["event_id"])

            with self.connect() as wconn:
                return outbox.consume(
                    wconn,
                    consumer="indexer.v1",
                    handler=handler,
                    topics=["doc.parsed"],
                    batch_size=4,
                )

        # Also test direct conflict on outbox_consumptions when two transactions
        # reach INSERT INTO outbox_consumptions for the same event_id.
        with ThreadPoolExecutor(max_workers=6) as pool:
            counts = list(pool.map(lambda _: worker_fn(), range(12)))

        self.assertEqual(sum(counts), 10)
        self.assertEqual(len(handled), 10)
        self.assertEqual(set(handled), set(event_ids))

    def test_outbox_on_conflict_do_nothing_when_preclaimed_mid_flight(self):
        """Even if another worker inserts into outbox_consumptions after SELECT,
        ON CONFLICT DO NOTHING skips the handler and does not fail the batch."""
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            with conn.transaction():
                op_id = self._insert_operation(conn, project_id)
                ev1 = self._insert_outbox_event(conn, project_id, op_id, "race.topic")
                ev2 = self._insert_outbox_event(conn, project_id, op_id, "race.topic")

            seen: list[uuid.UUID] = []

            def handler(c, ev):
                seen.append(ev["event_id"])
                # While processing ev1, simulate a concurrent transaction having
                # already recorded ev2 in outbox_consumptions (via autonomous conn
                # before ev2's INSERT runs).
                if ev["event_id"] == ev1:
                    with self.connect(autocommit=True) as other:
                        other.execute(
                            "INSERT INTO outbox_consumptions (consumer, event_id) VALUES (%s, %s)",
                            ("race.consumer", ev2),
                        )

            processed = outbox.consume(
                conn,
                consumer="race.consumer",
                handler=handler,
                topics=["race.topic"],
                batch_size=10,
            )

        self.assertEqual(processed, 1)
        self.assertEqual(seen, [ev1])

    def test_outbox_skip_event_ids_and_rollback_on_failure(self):
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            with conn.transaction():
                op_id = self._insert_operation(conn, project_id)
                poison_id = self._insert_outbox_event(conn, project_id, op_id, "t.poison")
                good_id = self._insert_outbox_event(conn, project_id, op_id, "t.poison")

            def failing_handler(_c, ev):
                if ev["event_id"] == poison_id:
                    raise RuntimeError("poison pill")

            with self.assertRaises(RuntimeError):
                outbox.consume(
                    conn,
                    consumer="c.poison",
                    handler=failing_handler,
                    topics=["t.poison"],
                    batch_size=10,
                    max_attempts=3,
                )

            # Neither event was marked consumed because the batch rolled back.
            cnt = conn.execute(
                "SELECT count(*) FROM outbox_consumptions WHERE consumer = 'c.poison'"
            ).fetchone()[0]
            self.assertEqual(cnt, 0)

            # Passing skip_event_ids isolates the poison message and processes good_id.
            processed_ids: list[uuid.UUID] = []
            n = outbox.consume(
                conn,
                consumer="c.poison",
                handler=lambda _c, ev: processed_ids.append(ev["event_id"]),
                topics=["t.poison"],
                batch_size=10,
                max_attempts=3,
                skip_event_ids={poison_id},
            )
            self.assertEqual(n, 1)
            self.assertEqual(processed_ids, [good_id])

            with self.assertRaises(ValueError):
                outbox.consume(
                    conn,
                    consumer="c.poison",
                    handler=lambda _c, _ev: None,
                    topics=["t.poison"],
                    max_attempts=0,
                )

    # ------------------------------------------------------------------
    # 2. artifacts.collect_staged tests
    # ------------------------------------------------------------------
    def test_collect_staged_dynamic_fk_legal_hold_and_savepoint_skip(self):
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)

            # 1) Unreferenced staged standard artifact -> should be deleted
            del_id = artifacts.stage(
                conn, project_id, "sha256/aa/" + "a" * 64, "1", "a" * 64, 10, "application/pdf", "standard"
            )
            # 2) Staged legal_hold artifact -> must NOT be collected
            hold_id = artifacts.stage(
                conn, project_id, "sha256/bb/" + "b" * 64, "1", "b" * 64, 10, "application/pdf", "legal_hold"
            )
            # 3) Staged artifact referenced by a brand-new dynamically created table -> excluded by pg_catalog NOT EXISTS
            dyn_ref_id = artifacts.stage(
                conn, project_id, "sha256/cc/" + "c" * 64, "1", "c" * 64, 10, "application/pdf", "standard"
            )
            # 4) Staged artifact referenced concurrently after candidate selection -> caught by SAVEPOINT ForeignKeyViolation and added to skipped
            race_ref_id = artifacts.stage(
                conn, project_id, "sha256/dd/" + "d" * 64, "1", "d" * 64, 10, "application/pdf", "standard"
            )

            with conn.transaction():
                # created_at is immutable for the application (0007); simulate age
                # by skipping triggers in this setup transaction only.
                conn.execute("SET LOCAL session_replication_role = replica")
                conn.execute(
                    "UPDATE artifacts SET created_at = %s WHERE id = ANY(%s)",
                    (old_ts, [del_id, hold_id, dyn_ref_id, race_ref_id]),
                )
                conn.execute(
                    """
                    CREATE TABLE custom_Unknown_Artifact_Refs (
                        ref_id uuid PRIMARY KEY,
                        project_id uuid NOT NULL,
                        art_id uuid NOT NULL,
                        FOREIGN KEY (project_id, art_id) REFERENCES artifacts(project_id, id) ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO custom_Unknown_Artifact_Refs (ref_id, project_id, art_id) VALUES (%s, %s, %s)",
                    (uuid.uuid4(), project_id, dyn_ref_id),
                )
                # Also create a table whose FK constraint is installed or populated right before DELETE
                conn.execute(
                    """
                    CREATE TABLE late_artifact_refs (
                        id uuid PRIMARY KEY,
                        target_artifact_id uuid NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT
                    )
                    """
                )

            try:
                # Simulate a reference inserted into late_artifact_refs right after
                # collect_staged's candidate SELECT (before DELETE) by firing a trigger
                # on another table or by testing two cases:
                # Case A: dyn_ref_id is already referenced in custom_Unknown_Artifact_Refs
                # BEFORE collect_staged runs -> dynamically discovered via pg_catalog and
                # excluded by NOT EXISTS (remains intact, not aborted).
                # Case B: race_ref_id is referenced in late_artifact_refs during a
                # concurrent transaction or via a wrapper on conn.execute right after
                # the candidate SELECT -> DELETE raises ForeignKeyViolation inside the
                # SAVEPOINT and records race_ref_id in GcResult.skipped!
                orig_execute = conn.execute
                injected = False

                def execute_with_concurrent_ref(query, params=None, **kw):
                    nonlocal injected
                    cur = orig_execute(query, params, **kw)
                    q_text = query.as_string(conn) if hasattr(query, "as_string") else str(query)
                    if not injected and "FOR UPDATE OF a SKIP LOCKED" in q_text:
                        injected = True
                        orig_execute(
                            "INSERT INTO late_artifact_refs (id, target_artifact_id) VALUES (%s, %s)",
                            (uuid.uuid4(), race_ref_id),
                        )
                    return cur

                with mock.patch.object(conn, "execute", side_effect=execute_with_concurrent_ref):
                    res = artifacts.collect_staged(conn, datetime.timedelta(hours=1))

                self.assertIsInstance(res, artifacts.GcResult)
                self.assertEqual(res.deleted, [del_id])
                self.assertEqual(res.skipped, [race_ref_id])

                remaining = {
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM artifacts WHERE id = ANY(%s)",
                        ([del_id, hold_id, dyn_ref_id, race_ref_id],),
                    ).fetchall()
                }
                self.assertEqual(remaining, {hold_id, dyn_ref_id, race_ref_id})
            finally:
                with conn.transaction():
                    conn.execute("DROP TABLE IF EXISTS late_artifact_refs")
                    conn.execute("DROP TABLE IF EXISTS custom_Unknown_Artifact_Refs")

    # ------------------------------------------------------------------
    # 3. storage.store_and_register & put() tests
    # ------------------------------------------------------------------
    def test_store_and_register_concurrent_10_workers_and_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = storage.LocalArtifactStore(Path(tmpdir).resolve())
            payload = b"concurrent-artifact-payload-bytes"

            with self.connect() as conn:
                project_id, _ = self.make_project(conn)

            barrier = threading.Barrier(10)

            def worker(_i: int) -> uuid.UUID:
                with self.connect() as wconn:
                    barrier.wait()
                    return storage.store_and_register(
                        wconn,
                        store,
                        project_id,
                        payload,
                        mime="application/pdf",
                        retention="restricted",
                    )

            with ThreadPoolExecutor(max_workers=10) as pool:
                ids = list(pool.map(worker, range(10)))

            self.assertEqual(len(set(ids)), 1)
            art_id = ids[0]

            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT id, state::text, mime, retention FROM artifacts WHERE project_id = %s",
                    (project_id,),
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0], (art_id, "committed", "application/pdf", "restricted"))

                # Negative: different mime -> ValueError
                with self.assertRaises(ValueError):
                    storage.store_and_register(
                        conn,
                        store,
                        project_id,
                        payload,
                        mime="text/plain",
                        retention="restricted",
                    )

                # Negative: different retention -> ValueError
                with self.assertRaises(ValueError):
                    storage.store_and_register(
                        conn,
                        store,
                        project_id,
                        payload,
                        mime="application/pdf",
                        retention="standard",
                    )

                # Positive: pre-existing staged row with matching mime/retention gets verified and committed
                staged_bytes = b"pre-staged-artifact-bytes"
                stored_obj = store.put(staged_bytes)
                staged_id = artifacts.stage(
                    conn,
                    project_id,
                    stored_obj.object_key,
                    stored_obj.object_version,
                    stored_obj.sha256,
                    stored_obj.size_bytes,
                    "application/json",
                    retention="standard",
                )
                returned_id = storage.store_and_register(
                    conn,
                    store,
                    project_id,
                    staged_bytes,
                    mime="application/json",
                    retention="standard",
                )
                self.assertEqual(returned_id, staged_id)
                st = conn.execute(
                    "SELECT state::text FROM artifacts WHERE id = %s", (staged_id,)
                ).fetchone()[0]
                self.assertEqual(st, "committed")

                # Negative: tombstoned artifact raises ValueError
                artifacts.tombstone(conn, staged_id, reason="superseded")
                with self.assertRaises(ValueError):
                    storage.store_and_register(
                        conn,
                        store,
                        project_id,
                        staged_bytes,
                        mime="application/json",
                        retention="standard",
                    )

    def test_local_artifact_store_put_fsyncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = storage.LocalArtifactStore(Path(tmpdir).resolve())
            fsynced_fds: list[int] = []
            real_fsync = storage.os.fsync

            def tracking_fsync(fd: int) -> None:
                fsynced_fds.append(fd)
                real_fsync(fd)

            with mock.patch("lab_db.storage.os.fsync", side_effect=tracking_fsync):
                store.put(b"verify-dir-fsync")

            # Expect at least 2 fsync calls: 1 for temporary file, 1 for parent directory
            self.assertGreaterEqual(len(fsynced_fds), 2)

    # ------------------------------------------------------------------
    # 4. jobs (acquire max_attempts, expired takeover, project_id isolation)
    # ------------------------------------------------------------------
    def test_jobs_acquire_attempts_and_project_isolation(self):
        with self.connect() as conn:
            proj_a, _ = self.make_project(conn)
            proj_b, _ = self.make_project(conn)
            job_id = uuid.uuid4()

            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO jobs (id, project_id, kind, status, max_attempts, retryable)
                    VALUES (%s, %s, 'parse.pdf', 'queued', 2, true)
                    """,
                    (job_id, proj_a),
                )
                conn.execute(
                    """
                    INSERT INTO graph_threads (thread_id, project_id, job_id, workflow_revision)
                    VALUES (%s, %s, %s, 'rev-1')
                    """,
                    ("thread-1", proj_a, job_id),
                )

            # Negative: acquire with wrong project_id fails
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(conn, proj_b, job_id, "worker-wrong-proj")

            # 1st acquire (queued -> running): attempt becomes 1
            lease1 = jobs.acquire(conn, proj_a, job_id, "worker-1", ttl_seconds=60)
            row = conn.execute(
                "SELECT status::text, attempt FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            self.assertEqual(row, ("running", 1))

            # Heartbeat & save_checkpoint with wrong project_id must fail
            with self.assertRaises(jobs.LeaseLost):
                jobs.heartbeat(conn, proj_b, lease1)
            with self.assertRaises(jobs.LeaseLost):
                jobs.save_checkpoint(
                    conn, proj_b, lease1, "thread-1", "rev-1", 1, {"step": 1}, 1, "node_a"
                )

            # Save checkpoint with right project_id succeeds
            seq = jobs.save_checkpoint(
                conn, proj_a, lease1, "thread-1", "rev-1", 1, {"step": 1}, 1, "node_a"
            )
            self.assertEqual(seq, 1)

            # load_checkpoint with right project_id returns checkpoint; wrong project_id returns None
            cp_ok = jobs.load_checkpoint(conn, proj_a, "thread-1")
            self.assertIsNotNone(cp_ok)
            self.assertEqual(cp_ok["seq"], 1)
            self.assertEqual(cp_ok["last_node"], "node_a")
            self.assertIsNone(jobs.load_checkpoint(conn, proj_b, "thread-1"))

            # Expire lease1 and take over: attempt becomes 2 (which equals max_attempts=2)
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET lease_expires_at = now() - interval '10 seconds', "
                    "row_version = row_version + 1 WHERE id = %s",
                    (job_id,),
                )
            lease2 = jobs.acquire(conn, proj_a, job_id, "worker-2", ttl_seconds=60)
            self.assertNotEqual(lease1.token, lease2.token)
            row = conn.execute(
                "SELECT status::text, attempt FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            self.assertEqual(row, ("running", 2))

            # Expire lease2: attempt is now 2 == max_attempts, so another takeover must be rejected!
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET lease_expires_at = now() - interval '10 seconds', "
                    "row_version = row_version + 1 WHERE id = %s",
                    (job_id,),
                )
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(conn, proj_a, job_id, "worker-3", ttl_seconds=60)

            # Also if returned to queued when attempt == max_attempts, acquire still rejects
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET lease_expires_at = now() + interval '60 seconds', row_version = row_version + 1 WHERE id = %s",
                    (job_id,),
                )
            with self.assertRaises(jobs.LeaseLost):
                jobs.finish(conn, proj_b, lease2, "queued", "wrong project finish")
            jobs.finish(conn, proj_a, lease2, "queued", "yield back to queue")
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(conn, proj_a, job_id, "worker-3", ttl_seconds=60)
