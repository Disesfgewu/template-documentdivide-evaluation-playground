"""The parse progress writer (Issue #205): throttled, serialised, and it never raises. No database: a fake connection records what it is told."""
import unittest

from lab_api.parse_worker import _ProgressWriter, _accepts_progress


class FakeConn:
    def __init__(self, fail=False):
        self.writes, self.commits, self.closed, self.fail = [], 0, False, fail

    def execute(self, sql, params):
        if self.fail:
            raise OSError("down")
        self.writes.append(params)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ProgressWriterTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.conn, self.opened = Clock(), FakeConn(), 0
        def connect():
            self.opened += 1
            return self.conn
        self.writer = _ProgressWriter(connect, "P", "J", clock=self.clock)

    def test_the_first_report_is_written_then_at_most_one_write_per_interval(self):
        self.writer(0, 100)
        self.writer(10, 100)
        self.writer(20, 100)                       # within 2 s of the first: skipped
        self.clock.t += 2.5
        self.writer(30, 100)
        self.assertEqual([w[2] for w in self.conn.writes], [0, 30])
        self.assertEqual(self.opened, 1)           # one connection, reused

    def test_the_final_report_is_always_written_even_inside_the_interval(self):
        self.writer(50, 100)
        self.writer(100, 100)
        self.assertEqual([w[2] for w in self.conn.writes], [50, 100])
        self.assertEqual(self.conn.commits, 2)
        self.assertEqual(self.conn.writes[-1], ("J", "P", 100, 100))

    def test_a_failing_database_is_swallowed_and_the_next_report_reconnects(self):
        self.conn.fail = True
        self.writer(10, 100)                       # must not raise
        self.conn.fail = False
        self.clock.t += 5
        self.writer(40, 100)
        self.assertEqual([w[2] for w in self.conn.writes], [40])
        self.assertEqual(self.opened, 2)

    def test_close_closes_its_own_connection_and_is_idempotent(self):
        self.writer(1, 10)
        self.writer.close()
        self.writer.close()
        self.assertTrue(self.conn.closed)

    def test_a_run_double_without_on_progress_is_not_given_it(self):
        self.assertFalse(_accepts_progress(lambda data, python, cancelled, document_id, budget_ref, operation_id: None))
        self.assertTrue(_accepts_progress(lambda data, **kw: None))
        self.assertTrue(_accepts_progress(lambda data, *, on_progress=None, **rest: None))
        from lab_parser.batch_parse import parse_in_batches
        self.assertTrue(_accepts_progress(parse_in_batches))
