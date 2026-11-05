"""Real PostgreSQL/pgvector readiness tests (no SQLite, no fakes).

Requires LAB_TEST_DATABASE_URL: a URL for a role allowed to CREATE DATABASE
and CREATE EXTENSION on a server that ships pgvector (see deploy/compose.dev.yml).
scripts/check.sh refuses to run this suite without it (reported as BLOCKED).
"""
import os
import tempfile
import unittest
import uuid
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import psycopg
from fastapi.testclient import TestClient

from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.readiness import connect

ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")


def with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RealDatabaseReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        suffix = uuid.uuid4().hex[:10]
        cls.with_vec = f"lab_ready_vec_{suffix}"
        cls.without_vec = f"lab_ready_novec_{suffix}"
        # Registered first so a failure later in setUpClass still drops both.
        cls.addClassCleanup(cls._drop_databases)
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{cls.with_vec}"')
            c.execute(f'CREATE DATABASE "{cls.without_vec}"')
        with psycopg.connect(with_db(ADMIN_URL, cls.with_vec), autocommit=True) as c:
            c.execute("CREATE EXTENSION vector")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)

    @classmethod
    def _drop_databases(cls):
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            for db in (cls.with_vec, cls.without_vec):
                c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')

    def env(self, db_name):
        return {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": with_db(ADMIN_URL, db_name),
            "LAB_ARTIFACT_ROOT": self.tmp.name,
        }

    def readyz(self, db_name):
        with mock.patch.dict(os.environ, self.env(db_name), clear=True):
            return TestClient(create_app(load_settings())).get("/readyz")

    def test_ready_with_pgvector(self):
        r = self.readyz(self.with_vec)
        self.assertEqual(r.status_code, 200, r.text)
        db = r.json()["checks"]["database"]
        self.assertTrue(db["ok"])
        self.assertRegex(db["detail"], r"pgvector \d+\.\d+")
        # The extension really works: a cosine distance query round-trips.
        with psycopg.connect(with_db(ADMIN_URL, self.with_vec)) as c:
            d = c.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector").fetchone()[0]
        self.assertAlmostEqual(d, 1.0)

    def test_not_ready_without_pgvector(self):
        r = self.readyz(self.without_vec)
        self.assertEqual(r.status_code, 503)
        self.assertIn("not installed", r.json()["checks"]["database"]["detail"])

    def test_statements_are_time_bounded(self):
        env = {**self.env(self.with_vec), "LAB_DB_CONNECT_TIMEOUT_S": "1.5"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = load_settings()
        with connect(s) as c:
            self.assertEqual(c.execute("SHOW statement_timeout").fetchone()[0], "1500ms")
            with self.assertRaises(psycopg.errors.QueryCanceled):
                c.execute("SELECT pg_sleep(5)")

    def test_not_ready_for_missing_database(self):
        r = self.readyz("lab_ready_missing_" + uuid.uuid4().hex[:8])
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.json()["checks"]["database"]["ok"])


if __name__ == "__main__":
    unittest.main()
