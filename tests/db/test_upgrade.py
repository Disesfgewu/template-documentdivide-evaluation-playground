"""T50: database upgrade integration tests on a real PostgreSQL."""
import uuid

from psycopg import errors
from psycopg.types.json import Jsonb

from lab_db.migrate import Migration, MigrationError, discover, migrate
from lab_db.repo import Change, record_change

from .base import FreshDatabase, h
from .test_runs import RunFixture

FINGERPRINT_TABLES = (
    "resource_versions",
    "benchmark_revisions",
    "questions",
    "facts",
    "submissions",
    "retrieved_hits",
    "question_scores",
    "evaluations",
    "audit_events",
    "lineage_edges",
)


def _table_columns(conn, table: str) -> list[tuple[str, str, str, str, str | None]]:
    return conn.execute(
        "SELECT column_name, data_type, udt_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()


def _insert_adaptive(conn, table: str, values: dict[str, object]) -> None:
    cols_meta = _table_columns(conn, table)
    col_names = []
    col_vals = []
    for name, data_type, udt_name, is_nullable, default in cols_meta:
        if name in values:
            val = values[name]
            if data_type == "jsonb" and not isinstance(val, Jsonb) and val is not None:
                val = Jsonb(val)
            elif udt_name == "uuid" and isinstance(val, str):
                val = uuid.UUID(val)
            elif data_type in ("text", "character varying") and isinstance(val, uuid.UUID):
                val = str(val)
            col_names.append(name)
            col_vals.append(val)
        elif is_nullable == "NO" and default is None:
            if udt_name == "uuid":
                val = uuid.uuid4()
            elif data_type == "jsonb":
                val = Jsonb({})
            elif data_type == "boolean":
                val = True
            elif data_type in ("integer", "smallint", "bigint", "numeric", "double precision", "real"):
                val = 1
            else:
                val = "v1"
            col_names.append(name)
            col_vals.append(val)
    placeholders = ", ".join(["%s"] * len(col_names))
    cols_sql = ", ".join(col_names)
    conn.execute(f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})", tuple(col_vals))


class UpgradeTests(FreshDatabase):  # helpers only; no test methods
    migrate_to_head = False

    def _fingerprints(self, conn) -> dict[str, str]:
        out: dict[str, str] = {}
        for table in FINGERPRINT_TABLES:
            digest = conn.execute(
                f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t"
            ).fetchone()[0]
            self.assertIsNotNone(digest, f"expected rows in {table}")
            out[table] = digest
        return out


class TamperedUpgradeTests(UpgradeTests):
    """Each scenario has its own class and therefore its own fresh database,
    so neither depends on test execution order."""

    def test_tampered_migration_rejected(self):
        migrations = discover()
        migrate(self.url, "v-old", migrations=migrations[:3])
        tampered = [
            Migration(m.version, m.name, m.sql + "\n-- x") if m.version == 2 else m
            for m in migrations
        ]
        with self.assertRaises(MigrationError):
            migrate(self.url, "v-new", migrations=tampered)
        with self.connect() as c:
            rows = c.execute(
                "SELECT version, deployment FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(rows, [(1, "v-old"), (2, "v-old"), (3, "v-old")])



class UpgradeDataTests(UpgradeTests):
    def test_upgrade_preserves_data_and_guards(self):
        migrate(self.url, "v-old", migrations=discover()[:3])

        with self.connect() as c:
            fx = RunFixture(self, c)
            run = fx.run()
            att = fx.attempt(run, fx.q)
            sub = fx.submission(run, att, fx.q)

            doc_id = (
                getattr(fx, "doc1", None)
                or getattr(fx, "d1", None)
                or getattr(fx, "doc", None)
                or c.execute(
                    "SELECT id FROM document_versions WHERE project_id = %s ORDER BY id LIMIT 1",
                    (fx.p,),
                ).fetchone()[0]
            )
            art_id = c.execute(
                "SELECT artifact_id FROM document_versions WHERE id = %s",
                (doc_id,),
            ).fetchone()[0]
            doc_sha = c.execute(
                "SELECT sha256 FROM artifacts WHERE id = %s",
                (art_id,),
            ).fetchone()[0]

            with c.transaction():
                _insert_adaptive(
                    c,
                    "retrieved_hits",
                    {
                        "id": uuid.uuid4(),
                        "project_id": fx.p,
                        "run_id": run,
                        "attempt_id": att,
                        "benchmark_id": fx.bench,
                        "question_id": fx.q,
                        "submission_id": sub,
                        "hit_id": "h1",
                        "hit_key": "h1",
                        "rank": 1,
                        "content": "hit one",
                        "quote": "hit one",
                        "kind": "text",
                        "region_kind": "text",
                        "document_version_id": doc_id,
                        "document_id": doc_id,
                        "document_sha256": doc_sha,
                        "page_index": 0,
                        "x0": 0.1,
                        "y0": 0.2,
                        "x1": 0.8,
                        "y1": 0.9,
                        "bbox": [0.1, 0.2, 0.8, 0.9],
                        "regions": [
                            {
                                "document_id": str(doc_id),
                                "document_sha256": doc_sha,
                                "page_index": 0,
                                "bbox": [0.1, 0.2, 0.8, 0.9],
                                "kind": "text",
                                "quote": "hit one",
                            }
                        ],
                        "in_budget": True,
                        "within_budget": True,
                        "budget_violation": False,
                        "token_count": 10,
                        "tokens": 10,
                    },
                )

            ev = fx.evaluation(run)
            fx.score(ev, run, fx.q, sub)
            with c.transaction():
                c.execute("UPDATE evaluations SET status = 'published' WHERE id = %s", (ev,))

            # A real lineage edge: a derived resource version built from the benchmark.
            bench_rv = c.execute("SELECT resource_id FROM benchmark_revisions WHERE id = %s",
                                 (fx.bench,)).fetchone()[0]
            record_change(c, Change(fx.p, fx.a, "report.derive", "report", "report_1", 1, h("report"),
                                    "lab-c14n-1", "report.created", lineage=[(bench_rv, "derived_from")]))
            before = self._fingerprints(c)

        applied = migrate(self.url, "v-new")
        self.assertEqual(applied, [m.version for m in discover()[3:]])

        with self.connect() as c:
            mig_rows = c.execute(
                "SELECT version, deployment FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                mig_rows,
                [(m.version, "v-old" if m.version <= 3 else "v-new") for m in discover()],
            )

            after = self._fingerprints(c)
            self.assertEqual(before, after)

            q_cols = {r[0] for r in _table_columns(c, "questions")}
            q_col = "question_text" if "question_text" in q_cols else ("prompt" if "prompt" in q_cols else "ordinal")
            q_val = "tampered" if q_col != "ordinal" else 99
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute(f"UPDATE questions SET {q_col} = %s WHERE id = %s", (q_val, fx.q))

            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute(
                        "UPDATE submissions SET payload_hash = %s WHERE id = %s",
                        (h("tampered"), sub),
                    )

            qs_cols = [r[0] for r in _table_columns(c, "question_scores")]
            ev_col = "evaluation_id" if "evaluation_id" in qs_cols else qs_cols[0]
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute(
                        f"UPDATE question_scores SET status = 'pending' WHERE {ev_col} = %s",
                        (ev,),
                    )

            with c.transaction():
                _insert_adaptive(
                    c,
                    "jobs",
                    {
                        "id": uuid.uuid4(),
                        "project_id": fx.p,
                        "kind": "parse",
                        "max_attempts": 3,
                    },
                )

                op_id = uuid.uuid4()
                op_values: dict[str, object] = {
                    "id": op_id,
                    "project_id": fx.p,
                    "actor_id": fx.a,
                    "created_by": fx.a,
                    "kind": "parse",
                    "name": "parse",
                    "operation_name": "parse",
                    "component": "parser",
                    "service": "lab",
                    "status": "succeeded",
                }
                existing_op = c.execute("SELECT * FROM operations LIMIT 1").fetchone()
                if existing_op is not None:
                    op_col_names = [
                        d.name for d in c.execute("SELECT * FROM operations LIMIT 0").description
                    ]
                    base_op = dict(zip(op_col_names, existing_op))
                    for k in list(base_op):
                        if "idempotency" in k or "key" in k:
                            base_op[k] = None
                    base_op["id"] = op_id
                    base_op["project_id"] = fx.p
                    base_op["status"] = "succeeded"
                    op_values = base_op
                _insert_adaptive(c, "operations", op_values)

                c.execute(
                    "INSERT INTO parse_runs (id, project_id, document_version_id, parser_name, "
                    "package_version, output_schema, model_revision, requested_profile, "
                    "effective_profile, fallback_reason, raw_artifact_id, manifest_artifact_id, "
                    "page_index_map, cost_usd, status, operation_id) "
                    "VALUES (%s, %s, %s, 'mineru', '4.0.0', 'mineru-v4', 'v1', 'standard', "
                    "'standard', NULL, %s, %s, %s, 0, 'succeeded', %s)",
                    (uuid.uuid4(), fx.p, doc_id, art_id, art_id, Jsonb({"0": 0}), op_id),
                )

        self.assertEqual(migrate(self.url, "v-new"), [])
