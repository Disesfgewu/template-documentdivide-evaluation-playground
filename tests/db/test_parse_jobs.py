"""enqueue_parse_job on a real PostgreSQL: the per-version cap and its lock (Refs #7).

Deterministic: the first transaction holds the version's advisory lock (uncommitted
job) while a second one is started; the test proves the second one waits for the
first to finish, then sees its job and is refused. No timing guess decides the
outcome: the second thread signals when it is about to call, and the first commits
only after that signal plus a check that the second has not finished.
"""
import threading
import uuid

from lab_db import jobs
from lab_db.repo import Change, record_change

from .base import FreshDatabase, h


class EnqueueParseJobTests(FreshDatabase):
    def document(self, conn, project, actor, pages=3):
        logical = f"doc-{uuid.uuid4().hex[:8]}"
        art, dv = uuid.uuid4(), uuid.uuid4()
        rv = record_change(conn, Change(project, actor, "document.import", "document_version", logical, 1,
                                        h(logical), "raw-bytes", "document.imported"))
        with conn.transaction():
            conn.execute("INSERT INTO artifacts (id, project_id, object_key, object_version, sha256, size_bytes, mime, "
                         "retention, state, committed_at) VALUES (%s, %s, %s, 'v1', %s, 100, 'application/pdf', "
                         "'restricted', 'committed', now())", (art, project, f"pdf/{art}", h(logical)))
            conn.execute("INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (dv, project, rv, art, h(logical), pages))
        return dv

    def operation(self, conn, project, actor):
        op = uuid.uuid4()
        with conn.transaction():
            conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status) "
                         "VALUES (%s, %s, 'document.parse', %s, 'intent')", (op, project, actor))
        return op

    def enqueue(self, conn, project, actor, dv):
        return jobs.enqueue_parse_job(conn, project_id=project, actor_id=actor, operation_id=self.operation(conn, project, actor),
                                      document_version_id=dv, profile="unstructured-oss-fast")

    def test_second_request_waits_for_the_first_then_is_refused_with_its_job_id(self):
        with self.connect() as setup:
            project, actor = self.make_project(setup)
            dv = self.document(setup, project, actor)
            op1 = self.operation(setup, project, actor)
            op2 = self.operation(setup, project, actor)
        first_in_lock = threading.Event()
        second_about_to_call = threading.Event()
        second_done = threading.Event()
        outcome = {}

        def second():
            with self.connect() as c2:
                second_about_to_call.set()
                try:
                    outcome["job"] = jobs.enqueue_parse_job(
                        c2, project_id=project, actor_id=actor, operation_id=op2,
                        document_version_id=dv, profile="unstructured-oss-fast")
                except jobs.ParseJobActive as exc:
                    outcome["refused"] = exc.job_id
                finally:
                    second_done.set()

        with self.connect() as c1:
            with c1.transaction():
                first_job = jobs.enqueue_parse_job(c1, project_id=project, actor_id=actor, operation_id=op1,
                                                   document_version_id=dv, profile="unstructured-oss-fast")
                first_in_lock.set()  # the lock is held and the first job is not committed yet
                thread = threading.Thread(target=second)
                thread.start()
                self.assertTrue(second_about_to_call.wait(10))
                # The second call can only be blocked on the lock: it cannot finish while c1 holds it.
                self.assertFalse(second_done.wait(1.0), "second request did not wait for the lock")
            # c1 committed on leaving the transaction block
            thread.join(10)
        self.assertTrue(second_done.is_set())
        self.assertEqual(outcome, {"refused": first_job})
        with self.connect() as check:
            self.assertEqual(check.execute("SELECT count(*) FROM parse_jobs WHERE project_id = %s", (project,)).fetchone()[0], 1)

    def test_terminal_states_free_the_version_and_other_versions_are_independent(self):
        with self.connect() as c:
            project, actor = self.make_project(c)
            dv, other = self.document(c, project, actor), self.document(c, project, actor)
            first = self.enqueue(c, project, actor, dv)
            with self.assertRaises(jobs.ParseJobActive) as active:
                self.enqueue(c, project, actor, dv)
            self.assertEqual(active.exception.job_id, first)
            self.enqueue(c, project, actor, other)  # independent version
            for status_path in (("succeeded",), ("cancelled",)):
                lease = jobs.acquire(c, project, first, "w1") if status_path == ("succeeded",) else None
                if lease is not None:
                    with self.assertRaises(jobs.ParseJobActive):
                        self.enqueue(c, project, actor, dv)  # running still owns it
                    jobs.finish(c, project, lease, "succeeded", "done")
                    first = self.enqueue(c, project, actor, dv)
                else:
                    with c.transaction():
                        c.execute("UPDATE jobs SET status = 'cancelled', row_version = row_version + 1 WHERE id = %s", (first,))
                    first = self.enqueue(c, project, actor, dv)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_jobs WHERE document_version_id = %s", (dv,)).fetchone()[0], 3)
