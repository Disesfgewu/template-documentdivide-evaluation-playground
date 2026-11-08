"""Alembic wiring that needs no database: revisions match the SQL files,
malformed script directories are refused, URL handling, checksum
verification and adoption of databases migrated by the earlier runner."""
import tempfile
import unittest
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from lab_db import migrate as m
from lab_db import schema_env as env

DB_DIR = Path(m.__file__).resolve().parents[1]


def script_for(migrations, mutate=None) -> ScriptDirectory:
    """A script directory built from ``migrations`` (optionally edited)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "schema"
    m._ephemeral_script_dir(migrations, root)
    if mutate:
        mutate(root / "versions")
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    script = ScriptDirectory.from_config(cfg)
    script._tmp = tmp  # keep the directory alive with the object
    return script


class RepositoryRevisionTests(unittest.TestCase):
    def test_revisions_apply_exactly_the_sql_files_in_order(self):
        """A SQL file without a revision, or a revision naming the wrong file,
        would silently skip or change a migration."""
        cfg = Config(str(DB_DIR / "alembic.ini"))
        script = ScriptDirectory.from_config(cfg)
        self.assertEqual(Path(script.dir).resolve(), m.SCHEMA_DIR.resolve())
        got = env.script_migrations(script)
        want = m.discover()
        self.assertEqual([(x.version, x.name, x.checksum) for x in got], [(x.version, x.name, x.checksum) for x in want])
        self.assertEqual(script.get_heads(), [f"{len(want):04d}"])

    def test_revision_ids_and_chain(self):
        script = ScriptDirectory.from_config(Config(str(DB_DIR / "alembic.ini")))
        revs = list(reversed(list(script.walk_revisions())))
        self.assertEqual([r.revision for r in revs], [f"{i:04d}" for i in range(1, len(revs) + 1)])
        self.assertIsNone(revs[0].down_revision)
        self.assertEqual([r.down_revision for r in revs[1:]], [r.revision for r in revs[:-1]])

    def test_downgrade_is_refused(self):
        script = ScriptDirectory.from_config(Config(str(DB_DIR / "alembic.ini")))
        for rev in script.walk_revisions():
            with self.assertRaises(NotImplementedError):
                rev.module.downgrade()


class MalformedScriptDirectoryTests(unittest.TestCase):
    def migrations(self, n=3):
        return [m.Migration(i, f"{i:04d}_x.sql", f"SELECT {i};") for i in range(1, n + 1)]

    def test_a_well_formed_directory_is_accepted(self):
        got = env.script_migrations(script_for(self.migrations()))
        self.assertEqual([x.version for x in got], [1, 2, 3])

    def test_a_missing_middle_revision_is_refused(self):
        script = script_for(self.migrations(), lambda v: (v / "0002.py").unlink())
        with self.assertRaisesRegex(m.MigrationError, "revision chain is broken"):
            env.script_migrations(script)

    def test_two_heads_are_refused(self):
        def fork(versions):
            (versions / "0003b.py").write_text(
                (versions / "0003.py").read_text().replace('revision = "0003"', 'revision = "0003b"'))
        with self.assertRaisesRegex(m.MigrationError, "several Alembic heads"):
            env.script_migrations(script_for(self.migrations(), fork))

    def test_a_revision_id_that_does_not_match_its_version_is_refused(self):
        def rename(versions):
            p = versions / "0002.py"
            p.write_text(p.read_text().replace('revision = "0002"', 'revision = "0002x"'))
            q = versions / "0003.py"
            q.write_text(q.read_text().replace("down_revision = '0002'", "down_revision = '0002x'"))
        with self.assertRaisesRegex(m.MigrationError, "does not match migration"):
            env.script_migrations(script_for(self.migrations(), rename))

    def test_a_revision_without_a_migration_is_refused(self):
        def strip(versions):
            p = versions / "0002.py"
            p.write_text("\n".join(l for l in p.read_text().splitlines() if not l.startswith("MIGRATION")) + "\n")
        with self.assertRaisesRegex(m.MigrationError, "has no MIGRATION"):
            env.script_migrations(script_for(self.migrations(), strip))

    def test_a_chain_not_starting_at_one_is_refused(self):
        migs = [m.Migration(2, "0002_x.sql", "SELECT 2;"), m.Migration(3, "0003_x.sql", "SELECT 3;")]
        script = script_for(migs, lambda v: None)
        with self.assertRaisesRegex(m.MigrationError, "revision chain is broken"):
            env.script_migrations(script)


class UrlTests(unittest.TestCase):
    def test_postgres_urls_become_psycopg_urls(self):
        self.assertEqual(env._sqlalchemy_url("postgresql://u:p@h:5432/d?sslmode=require"),
                         "postgresql+psycopg://u:p@h:5432/d?sslmode=require")
        self.assertEqual(env._sqlalchemy_url("postgres://u@h/d"), "postgresql+psycopg://u@h/d")
        self.assertEqual(env._sqlalchemy_url("postgresql+psycopg://u@h/d"), "postgresql+psycopg://u@h/d")

    def test_other_or_missing_urls_are_refused_without_echoing_them(self):
        for bad in (None, "", "sqlite:///x.db", "mysql://u:secret@h/d", "postgresql+asyncpg://u:secret@h/d"):
            with self.subTest(bad=bad):
                with self.assertRaises(m.MigrationError) as ctx:
                    env._sqlalchemy_url(bad)
                self.assertNotIn("secret", str(ctx.exception))


class FakeRaw:
    """Just enough of a psycopg connection for verify_and_adopt."""

    def __init__(self, recorded=None, stamped=False):
        self.recorded, self.stamped, self.statements = recorded, stamped, []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        outer = self

        class Result:
            def fetchone(self_inner):
                if "schema_migrations')" in sql:
                    return (None if outer.recorded is None else "schema_migrations",)
                if "alembic_version')" in sql:
                    return ("alembic_version" if outer.stamped else None,)
                raise AssertionError(sql)

            def fetchall(self_inner):
                return list(outer.recorded)

        return Result()

    def wrote(self, text):
        return [s for s in self.statements if s[0].startswith(text)]


class VerifyAndAdoptTests(unittest.TestCase):
    def setUp(self):
        self.migs = [m.Migration(i, f"{i:04d}_x.sql", f"SELECT {i};") for i in range(1, 6)]

    def rows(self, upto, tamper=None):
        return [(x.version, "0" * 64 if x.version == tamper else x.checksum) for x in self.migs[:upto]]

    def test_a_fresh_database_is_left_alone(self):
        raw = FakeRaw(recorded=None)
        env.verify_and_adopt(raw, self.migs)
        self.assertEqual(raw.wrote("CREATE"), [])

    def test_a_database_migrated_by_the_earlier_runner_is_stamped_not_rerun(self):
        raw = FakeRaw(recorded=self.rows(3), stamped=False)
        env.verify_and_adopt(raw, self.migs)
        self.assertEqual(len(raw.wrote("CREATE TABLE alembic_version")), 1)
        self.assertEqual(raw.wrote("INSERT INTO alembic_version"), [("INSERT INTO alembic_version (version_num) VALUES (%s)", ("0003",))])

    def test_an_already_stamped_database_is_not_stamped_again(self):
        raw = FakeRaw(recorded=self.rows(3), stamped=True)
        env.verify_and_adopt(raw, self.migs)
        self.assertEqual(raw.wrote("CREATE"), [])
        self.assertEqual(raw.wrote("INSERT"), [])

    def test_empty_bookkeeping_is_not_stamped(self):
        raw = FakeRaw(recorded=[], stamped=False)
        env.verify_and_adopt(raw, self.migs)
        self.assertEqual(raw.wrote("CREATE"), [])

    def test_a_changed_migration_is_refused_even_when_stamped(self):
        for stamped in (False, True):
            with self.subTest(stamped=stamped):
                raw = FakeRaw(recorded=self.rows(3, tamper=2), stamped=stamped)
                with self.assertRaisesRegex(m.MigrationError, "0002_x.sql was changed after being applied"):
                    env.verify_and_adopt(raw, self.migs)
                self.assertEqual(raw.wrote("CREATE"), [])

    def test_a_database_ahead_of_this_code_is_refused(self):
        raw = FakeRaw(recorded=self.rows(5), stamped=True)
        with self.assertRaisesRegex(m.MigrationError, "which this code does not contain"):
            env.verify_and_adopt(raw, self.migs[:3])

    def test_recorded_versions_with_a_gap_are_not_adopted(self):
        raw = FakeRaw(recorded=[self.rows(5)[0], self.rows(5)[2]], stamped=False)
        with self.assertRaisesRegex(m.MigrationError, "not 1..n"):
            env.verify_and_adopt(raw, self.migs)
        self.assertEqual(raw.wrote("CREATE"), [])


class EnvironmentGuardTests(unittest.TestCase):
    def test_offline_mode_and_missing_deployment_are_refused(self):
        src = (Path(env.__file__)).read_text()
        self.assertIn("offline (--sql) mode is not supported", src)
        self.assertIn("a deployment id is required", src)


if __name__ == "__main__":
    unittest.main()
