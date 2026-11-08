import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg

from lab_db import migrate as migrate_module
from lab_db.migrate import BOOKKEEPING, MigrationError, discover, migrate

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


DB_DIR = Path(migrate_module.__file__).resolve().parents[1]


class AlembicVersionTests(FreshDatabase):
    migrate_to_head = False

    def test_alembic_tracks_the_head_and_agrees_with_the_bookkeeping(self):
        migrate(self.url, deployment="t1")
        head = f"{len(discover()):04d}"
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchall(), [(head,)])
            self.assertEqual(c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], len(discover()))


class AdoptionTests(FreshDatabase):
    migrate_to_head = False

    def test_database_migrated_by_the_earlier_runner_is_adopted_not_rerun(self):
        """The earlier runner recorded schema_migrations only. Alembic must verify
        it, stamp it and apply just the rest: re-running 0001 would fail on
        existing tables."""
        legacy = discover()[:5]
        with psycopg.connect(self.url) as c:
            c.execute(BOOKKEEPING)
            for mig in legacy:
                c.execute(mig.sql)
                c.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, 'legacy')",
                          (mig.version, mig.name, mig.checksum))
            c.commit()
            self.assertIsNone(c.execute("SELECT to_regclass('alembic_version')").fetchone()[0])

        applied = migrate(self.url, deployment="adopted")
        self.assertEqual(applied, [m.version for m in discover()[5:]])
        with self.connect() as c:
            rows = c.execute("SELECT version, deployment FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual(rows, [(m.version, "legacy" if m.version <= 5 else "adopted") for m in discover()])
            self.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchall(), [(f"{len(discover()):04d}",)])
        self.assertEqual(migrate(self.url, deployment="again"), [])


class AdoptionRefusalTests(FreshDatabase):
    migrate_to_head = False

    def test_adoption_refuses_a_legacy_database_whose_migration_was_edited(self):
        with psycopg.connect(self.url) as c:
            c.execute(BOOKKEEPING)
            for mig in discover()[:2]:
                c.execute(mig.sql)
                bad = "0" * 64 if mig.version == 2 else mig.checksum
                c.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, 'legacy')",
                          (mig.version, mig.name, bad))
            c.commit()
        with self.assertRaisesRegex(MigrationError, "changed after being applied"):
            migrate(self.url, deployment="adopted")
        with self.connect() as c:
            self.assertIsNone(c.execute("SELECT to_regclass('alembic_version')").fetchone()[0])


class AlembicCommandLineTests(FreshDatabase):
    migrate_to_head = False

    def alembic(self, *args, url=True, deployment="cli"):
        env = {k: v for k, v in os.environ.items() if k != "LAB_MIGRATION_DATABASE_URL"}
        env["PYTHONPATH"] = str(DB_DIR)
        if url:
            env["LAB_MIGRATION_DATABASE_URL"] = self.url
        cmd = [sys.executable, "-m", "alembic", "-c", str(DB_DIR / "alembic.ini")]
        if deployment:
            cmd += ["-x", f"deployment={deployment}"]
        with tempfile.TemporaryDirectory() as cwd:  # works from any directory
            return subprocess.run(cmd + list(args), env=env, cwd=cwd, capture_output=True, text=True, timeout=300)

    def test_alembic_upgrade_head_from_the_command_line(self):
        missing = self.alembic("upgrade", "head", deployment=None)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("a deployment id is required", missing.stderr)
        no_url = self.alembic("upgrade", "head", url=False)
        self.assertNotEqual(no_url.returncode, 0)
        self.assertIn("LAB_MIGRATION_DATABASE_URL", no_url.stderr)
        with self.connect() as c:
            self.assertIsNone(c.execute("SELECT to_regclass('schema_migrations')").fetchone()[0])

        first = self.alembic("upgrade", "head")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.alembic("upgrade", "head").returncode, 0)
        with self.connect() as c:
            rows = c.execute("SELECT version, deployment FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual(rows, [(m.version, "cli") for m in discover()])
        current = self.alembic("current")
        self.assertIn(f"{len(discover()):04d}", current.stdout + current.stderr)
        self.assertNotIn(self.url, first.stdout + first.stderr)  # the URL is never echoed

        down = self.alembic("downgrade", "-1")
        self.assertNotEqual(down.returncode, 0)  # forward-only
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], len(discover()))


class ConcurrentMigratorTests(FreshDatabase):
    migrate_to_head = False

    def test_concurrent_migrators_apply_every_migration_exactly_once(self):
        """Separate processes, as real migrators are (Alembic's context is
        process-global, so threads in one process cannot run concurrently)."""
        import ast

        env = dict(os.environ, PYTHONPATH=str(DB_DIR), LAB_MIGRATION_DATABASE_URL=self.url)
        procs = [subprocess.Popen([sys.executable, "-m", "lab_db.migrate", "--deployment", f"c{i}"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(3)]
        applied = []
        for proc in procs:
            out, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, err)
            self.assertTrue(out.startswith("applied: "), out)
            text = out[len("applied: "):].strip()
            applied.append([] if text.startswith("none") else ast.literal_eval(text))
        self.assertEqual(sorted(v for r in applied for v in r), [m.version for m in discover()])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*), count(DISTINCT version) FROM schema_migrations").fetchone(),
                             (len(discover()), len(discover())))


class InconsistentStateAbsentSchemaMigrationsTests(FreshDatabase):
    migrate_to_head = False

    def test_alembic_version_stamped_ahead_of_absent_schema_migrations_is_refused(self):
        with psycopg.connect(self.url) as c:
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            c.execute("INSERT INTO alembic_version (version_num) VALUES ('0003')")
            c.commit()
        with self.assertRaisesRegex(MigrationError, "schema_migrations does not exist"):
            migrate(self.url, deployment="test")
        with self.connect() as c:
            self.assertIsNone(c.execute("SELECT to_regclass('schema_migrations')").fetchone()[0])


class InconsistentStateIncompleteSchemaMigrationsTests(FreshDatabase):
    migrate_to_head = False

    def test_alembic_version_stamped_ahead_of_incomplete_schema_migrations_is_refused(self):
        legacy = discover()[:2]
        with psycopg.connect(self.url) as c:
            c.execute(BOOKKEEPING)
            for mig in legacy:
                c.execute(mig.sql)
                c.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, 'init')",
                          (mig.version, mig.name, mig.checksum))
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            c.execute("INSERT INTO alembic_version (version_num) VALUES ('0004')")
            c.commit()
        with self.assertRaisesRegex(MigrationError, "stamped at 0004, but schema_migrations is at 0002"):
            migrate(self.url, deployment="test")


class InconsistentStateGappedSchemaMigrationsTests(FreshDatabase):
    migrate_to_head = False

    def test_gapped_schema_migrations_with_stamped_alembic_version_is_refused(self):
        migs = discover()
        with psycopg.connect(self.url) as c:
            c.execute(BOOKKEEPING)
            # Apply dependent DDL in order, but simulate a damaged ledger
            # whose middle version is missing.
            for mig in migs[:3]:
                c.execute(mig.sql)
                if mig.version != 2:
                    c.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, 'init')",
                              (mig.version, mig.name, mig.checksum))
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            c.execute("INSERT INTO alembic_version (version_num) VALUES ('0003')")
            c.commit()
        with self.assertRaisesRegex(MigrationError, "recorded migrations are not 1..n"):
            migrate(self.url, deployment="test")


class InconsistentStateMismatchedTests(FreshDatabase):
    migrate_to_head = False

    def test_mismatched_alembic_version_behind_schema_migrations_is_refused(self):
        migs = discover()[:3]
        with psycopg.connect(self.url) as c:
            c.execute(BOOKKEEPING)
            for mig in migs:
                c.execute(mig.sql)
                c.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, 'init')",
                          (mig.version, mig.name, mig.checksum))
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            c.execute("INSERT INTO alembic_version (version_num) VALUES ('0002')")
            c.commit()
        with self.assertRaisesRegex(MigrationError, "stamped at 0002, but schema_migrations is at 0003"):
            migrate(self.url, deployment="test")


class IntermediateHeadTests(FreshDatabase):
    migrate_to_head = False

    def test_intermediate_head_is_preserved_and_upgraded_to_head(self):
        intermediate = discover()[:3]
        applied = migrate(self.url, deployment="first", migrations=intermediate)
        self.assertEqual(applied, [1, 2, 3])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchall(), [("0003",)])
            rows = c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual([r[0] for r in rows], [1, 2, 3])

        remaining = migrate(self.url, deployment="second")
        self.assertEqual(remaining, [m.version for m in discover()[3:]])
        with self.connect() as c:
            head = f"{len(discover()):04d}"
            self.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchall(), [(head,)])
            rows = c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual([r[0] for r in rows], [m.version for m in discover()])
        self.assertEqual(migrate(self.url, deployment="idempotent"), [])
