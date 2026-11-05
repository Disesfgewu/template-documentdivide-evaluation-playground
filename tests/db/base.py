"""Shared fixture: a fresh real PostgreSQL database per test class, migrated
to head. Requires LAB_TEST_DATABASE_URL (superuser-capable role, see
deploy/compose.dev.yml); scripts/check.sh reports BLOCKED without it."""
import hashlib
import os
import unittest
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg

from lab_db.migrate import migrate

ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")


def with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class FreshDatabase(unittest.TestCase):
    migrate_to_head = True

    @classmethod
    def setUpClass(cls):
        cls.dbname = f"lab_t_{uuid.uuid4().hex[:12]}"
        cls.addClassCleanup(cls._drop)
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{cls.dbname}"')
        cls.url = with_db(ADMIN_URL, cls.dbname)
        if cls.migrate_to_head:
            migrate(cls.url, deployment="test")

    @classmethod
    def _drop(cls):
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{cls.dbname}" WITH (FORCE)')

    def connect(self, **kw):
        return psycopg.connect(self.url, **kw)

    def make_project(self, conn, role="owner"):
        """A project with one actor holding ``role``; returns (project_id, actor_id)."""
        p, a = uuid.uuid4(), uuid.uuid4()
        with conn.transaction():
            conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (p, "p"))
            conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'u')", (a,))
            conn.execute("INSERT INTO project_members VALUES (%s, %s, %s)", (p, a, role))
        return p, a

    def add_member(self, conn, project_id, role):
        a = uuid.uuid4()
        with conn.transaction():
            conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'u')", (a,))
            conn.execute("INSERT INTO project_members VALUES (%s, %s, %s)", (project_id, a, role))
        return a
