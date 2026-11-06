import tempfile
import uuid
from pathlib import Path

import psycopg

from lab_db.migrate import MigrationError, discover, migrate

from .base import FreshDatabase


class MigrationTests(FreshDatabase):
    migrate_to_head = False

    def test_apply_record_and_rerun(self):
        first = migrate(self.url, deployment="t1")
        self.assertEqual(first, [m.version for m in discover()])
        self.assertEqual(migrate(self.url, deployment="t2"), [])  # idempotent
        with self.connect() as c:
            rows = c.execute("SELECT version, checksum, deployment FROM schema_migrations ORDER BY version").fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows],
                         [(m.version, m.checksum, "t1") for m in discover()])

    def test_changed_migration_refused(self):
        migrate(self.url, deployment="t1")
        tampered = [m.__class__(m.version, m.name, m.sql + "\n-- edited") for m in discover()]
        with self.assertRaises(MigrationError):
            migrate(self.url, deployment="t3", migrations=tampered)

    def test_failing_migration_rolls_back_completely(self):
        migrate(self.url, deployment="t1")
        with tempfile.TemporaryDirectory() as d:
            for m in discover():
                (Path(d) / m.name).write_text(m.sql)
            nxt = len(discover()) + 1
            (Path(d) / f"{nxt:04d}_broken.sql").write_text(
                "CREATE TABLE half_done (id int);\nSELECT 1/0;\n")
            with self.assertRaises(psycopg.Error):
                migrate(self.url, deployment="t4", migrations=discover(Path(d)))
        with self.connect() as c:
            self.assertIsNone(c.execute("SELECT to_regclass('half_done')").fetchone()[0])
            self.assertEqual(c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], nxt - 1)

    def test_gap_in_versions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "0001_a.sql").write_text("SELECT 1;")
            (Path(d) / "0003_c.sql").write_text("SELECT 1;")
            with self.assertRaises(MigrationError):
                discover(Path(d))
