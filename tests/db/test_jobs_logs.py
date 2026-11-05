"""0004: job state machine, leases/fencing, checkpoints, node operations and
redacted logs on a real PostgreSQL."""
import json
import time
import uuid

from psycopg import errors

from lab_db import jobs
from lab_db.logs import MASK, log_event, redact

from .base import FreshDatabase


class JobFixture:
    def __init__(self, test, conn, max_attempts=3, retryable=True):
        self.c = conn
        self.p, self.a = test.make_project(conn)
        self.job = uuid.uuid4()
        with conn.transaction():
            conn.execute("INSERT INTO jobs (id, project_id, kind, max_attempts, retryable) VALUES (%s, %s, 'parse', %s, %s)",
                         (self.job, self.p, max_attempts, retryable))

    def status(self):
        return self.c.execute("SELECT status::text FROM jobs WHERE id = %s", (self.job,)).fetchone()[0]

    def thread(self, tid="t-1", rev="wf@1"):
        with self.c.transaction():
            self.c.execute("INSERT INTO graph_threads (thread_id, project_id, job_id, workflow_revision) "
                           "VALUES (%s, %s, %s, %s)", (tid, self.p, self.job, rev))
        return tid


class JobStateTests(FreshDatabase):
    def test_spec_16_6_transitions(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            lease = jobs.acquire(c, f.job, "w1")
            jobs.finish(c, lease, "succeeded", "done")
            self.assertEqual(f.status(), "succeeded")
            # Terminal: a new request needs a new job.
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(c, f.job, "w2")
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE jobs SET status = 'queued', row_version = row_version + 1 WHERE id = %s", (f.job,))
            reasons = c.execute("SELECT from_status::text, to_status::text, reason FROM job_transitions "
                                "WHERE job_id = %s ORDER BY id", (f.job,)).fetchall()
            self.assertEqual([r[:2] for r in reasons], [("queued", "running"), ("running", "succeeded")])
            self.assertEqual(reasons[1][2], "done")

    def test_illegal_transitions_rejected(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            for target in ("succeeded", "failed", "paused_budget"):  # queued may only go to running/cancelled
                with self.subTest(target), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute("UPDATE jobs SET status = %s, row_version = row_version + 1 WHERE id = %s",
                                  (target, f.job))
            lease = jobs.acquire(c, f.job, "w1")
            jobs.finish(c, lease, "cancel_requested", "user cancel")
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():  # cancel_requested -> queued is not allowed
                    c.execute("UPDATE jobs SET status = 'queued', row_version = row_version + 1 WHERE id = %s", (f.job,))

    def test_retry_only_when_retryable_and_attempts_left(self):
        with self.connect() as c:
            f = JobFixture(self, c, max_attempts=2, retryable=True)
            for n in (1, 2):
                lease = jobs.acquire(c, f.job, "w")
                jobs.finish(c, lease, "failed", f"attempt {n} failed")
                if n == 1:
                    with c.transaction():
                        c.execute("UPDATE jobs SET status = 'queued', row_version = row_version + 1 WHERE id = %s", (f.job,))
            with self.assertRaises(errors.CheckViolation):  # attempts exhausted
                with c.transaction():
                    c.execute("UPDATE jobs SET status = 'queued', row_version = row_version + 1 WHERE id = %s", (f.job,))
            g = JobFixture(self, c, retryable=False)
            jobs.finish(c, jobs.acquire(c, g.job, "w"), "failed", "permanent")
            with self.assertRaises(errors.CheckViolation):
                with c.transaction():
                    c.execute("UPDATE jobs SET status = 'queued', row_version = row_version + 1 WHERE id = %s", (g.job,))


class FencingTests(FreshDatabase):
    def test_live_lease_blocks_second_worker(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            jobs.acquire(c, f.job, "w1", ttl_seconds=60)
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(c, f.job, "w2")

    def test_expired_lease_taken_over_and_old_worker_fenced(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            tid = f.thread()
            old = jobs.acquire(c, f.job, "w1", ttl_seconds=1)
            self.assertEqual(jobs.save_checkpoint(c, old, tid, "wf@1", 1, {"stage": "parse"}, 1, "parse_batch"), 1)
            time.sleep(1.5)
            new = jobs.acquire(c, f.job, "w2", ttl_seconds=60)
            self.assertGreater(new.token, old.token)
            for call in (lambda: jobs.save_checkpoint(c, old, tid, "wf@1", 1, {"stage": "stale"}, 2, "x"),
                         lambda: jobs.heartbeat(c, old),
                         lambda: jobs.finish(c, old, "succeeded", "stale worker")):
                with self.assertRaises(jobs.LeaseLost):
                    call()
            self.assertEqual(jobs.save_checkpoint(c, new, tid, "wf@1", 1, {"stage": "normalize"}, 2, "normalize"), 2)
            cp = jobs.load_checkpoint(c, tid)
            self.assertEqual((cp["seq"], cp["state"], cp["last_node"]), (2, {"stage": "normalize"}, "normalize"))
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE graph_checkpoints SET last_node = 'x'")

    def test_revision_change_needs_a_new_thread(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            tid = f.thread(rev="wf@1")
            lease = jobs.acquire(c, f.job, "w1")
            with self.assertRaises(jobs.GraphRevisionChanged):
                jobs.save_checkpoint(c, lease, tid, "wf@2", 1, {}, 1, "x")

    def test_checkpoint_state_round_trips_types(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            tid = f.thread()
            lease = jobs.acquire(c, f.job, "w1")
            state = {"attempt": 3, "ratio": 0.5, "refs": ["a", "b"], "nested": {"n": None, "ok": True}}
            jobs.save_checkpoint(c, lease, tid, "wf@1", 1, state, 1, "n")
            got = jobs.load_checkpoint(c, tid)["state"]
            self.assertEqual(got, state)
            self.assertIsInstance(got["attempt"], int)


class NodeOperationTests(FreshDatabase):
    def test_one_success_per_logical_invocation(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            inv = uuid.uuid4()
            arts = []
            for _ in range(2):
                art = uuid.uuid4()
                with c.transaction():
                    c.execute("INSERT INTO artifacts (id, project_id, object_key, object_version, sha256, size_bytes, "
                              "mime, retention, state, committed_at) VALUES (%s, %s, %s, 'v1', %s, 1, "
                              "'application/json', 'restricted', 'committed', now())",
                              (art, f.p, f"op/{art}", "a" * 64))
                arts.append(art)
            ins = ("INSERT INTO node_operations (id, job_id, work_item_id, node_revision, logical_invocation_id, "
                   "attempt, status, result_artifact_id) VALUES (%s, %s, 'page-0-9', 'parse@1', %s, %s, %s, %s)")
            with c.transaction():
                c.execute(ins, (uuid.uuid4(), f.job, inv, 1, "outcome_unknown", None))
                c.execute(ins, (uuid.uuid4(), f.job, inv, 2, "succeeded", arts[0]))
            with self.assertRaises(errors.UniqueViolation):
                with c.transaction():
                    c.execute(ins, (uuid.uuid4(), f.job, inv, 3, "succeeded", arts[1]))
            with self.assertRaises(errors.CheckViolation):
                with c.transaction():
                    c.execute(ins, (uuid.uuid4(), f.job, uuid.uuid4(), 1, "succeeded", None))


# Fake credentials are assembled at runtime so the repository secret scanner
# never sees a complete key-shaped literal.
AWS_FAKE = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
GH_FAKE = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuv"
SK_FAKE = "s" + "k-live-" + "0123456789abcdefghij"

SECRETS = [
    "Authorization: Bearer abcdefghijklmnop.qrstuv",
    "authorization=Basic dXNlcjpwYXNzd29yZA==",
    "postgresql://lab:hunter2-secret@db.internal:5432/lab",
    "https://bucket.s3.amazonaws.com/x.pdf?X-Amz-Signature=deadbeefcafe&X-Amz-Credential=" + AWS_FAKE,
    "api_key=" + SK_FAKE,
    "Cookie: session=0123456789abcdef",
    'config {"password": "pa55w0rd!", "token": "tok_123456789"}',
    GH_FAKE,
]
LEAKS = ["abcdefghijklmnop", "dXNlcjpwYXNzd29yZA", "hunter2", "deadbeefcafe", AWS_FAKE,
         SK_FAKE[:18], "0123456789abcdef", "pa55w0rd", "tok_123456789", GH_FAKE[:14]]


class RedactionTests(FreshDatabase):
    def test_t69_secrets_never_reach_log_rows(self):
        for s in SECRETS:
            with self.subTest(s):
                out = redact(s)
                self.assertIn(MASK, out)
                for leak in LEAKS:
                    self.assertNotIn(leak, out)
        with self.connect() as c:
            p, _ = self.make_project(c)
            eid = log_event(c, level="ERROR", event_name="operation.failed", message=" | ".join(SECRETS),
                            service="api", component="test", operation_name="t", project_id=p,
                            error_message=SECRETS[2],
                            attributes={"headers": {"Authorization": "Bearer zzzzzzzzzzzz", "x-request-id": "r1"},
                                        "url": SECRETS[3], "api_key": "plain-value-123"})
            c.commit()
            row = c.execute("SELECT message, error_message, attributes::text, redaction_policy FROM log_events "
                            "WHERE event_id = %s", (eid,)).fetchone()
            dump = json.dumps(row)
            for leak in LEAKS + ["zzzzzzzzzzzz", "plain-value-123"]:
                self.assertNotIn(leak, dump)
            self.assertIn('"r1"', row[2])  # non-secret attributes survive
            self.assertEqual(row[3], "lab-redact-1")

    def test_resend_is_deduplicated_and_rows_are_append_only(self):
        with self.connect() as c:
            eid = uuid.uuid4()
            for _ in range(2):
                log_event(c, level="INFO", event_name="parse.completed", message="ok", service="worker",
                          component="parse", operation_name="parse_batch", event_id=eid)
            c.commit()
            self.assertEqual(c.execute("SELECT count(*) FROM log_events WHERE event_id = %s", (eid,)).fetchone()[0], 1)
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE log_events SET message = 'x'")
            with self.assertRaises(ValueError):
                log_event(c, level="INFO", event_name="a.b", message="m", service="s", component="c",
                          operation_name="o", password="x")
