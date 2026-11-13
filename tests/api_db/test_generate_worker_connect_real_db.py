"""#174: the generate worker must run with the connection factory main() really uses (autocommit), against real PostgreSQL.

The earlier tests passed an ordinary helper connection, so the claim + budget reservation stayed in one open transaction on the
pick connection and the heartbeat connection (which every real run has) waited on its job-row lock: the job never ran."""
from __future__ import annotations

import inspect
import unittest

import psycopg

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_api import generate_worker_main as gwm

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class GenerateWorkerConnectRealDbTests(GenerateApiFixtures, FreshDatabase):
    def run_with(self, connect, once_timeout=60):
        project, owner = self.planned()
        self.assertEqual(self.api("post", owner, self.GEN, project, BODY).status_code, 202)
        # a call that takes 4 s with a 3 s lease: the heartbeat runs on its own connection while the call is in flight
        return gwm.GenerateWorker(connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="conn-test", interval=1, backoff=1, grace=5,
                                  once_timeout=once_timeout, ttl_seconds=3,
                                  extra_env={"FAKE_LAB_GEN_MODE": "questions", "FAKE_LAB_GEN_DELAY": "4"}).run_once()

    def test_the_factory_main_uses_is_autocommit_and_main_calls_it(self):
        with gwm.connect_factory(self.url)() as conn:
            self.assertTrue(conn.autocommit)
        self.assertIn("connect_factory(url)", inspect.getsource(gwm.main))

    def test_a_job_runs_to_the_end_with_the_production_connection_factory_while_the_heartbeat_beats(self):
        counts = self.run_with(gwm.connect_factory(self.url))
        self.assertEqual(counts.get("succeeded:generated"), 1, counts)

    def test_the_old_factory_without_autocommit_cannot_complete_the_job_this_is_why_the_test_above_exists(self):
        old = lambda: psycopg.connect(self.url, connect_timeout=5, options="-c lock_timeout=3s")      # the factory #174 found
        counts = self.run_with(old, once_timeout=40)
        self.assertNotEqual(counts.get("succeeded:generated"), 1, counts)


if __name__ == "__main__":
    unittest.main()
