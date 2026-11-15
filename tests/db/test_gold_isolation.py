"""Real PostgreSQL tests for gold RLS isolation (0008_gold_isolation.sql, SPEC 16.4, A18)."""
import contextlib
import re
import unittest
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from lab_db.repo import grant_app_privileges
from tests.db.base import FreshDatabase, h


class TestGoldIsolation(FreshDatabase):
    """Verify RLS on facts, evidence_sets, evidence_set_members, and distractors."""

    _seq = 0

    @classmethod
    def _next_seq(cls) -> int:
        cls._seq += 1
        return cls._seq

    def make_app_role(self, conn: psycopg.Connection) -> str:
        role = f"lab_app_{uuid.uuid4().hex[:10]}"
        with conn.transaction():
            conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        grant_app_privileges(conn, role)
        conn.commit()

        def _cleanup() -> None:
            with self.connect(autocommit=True) as c:
                c.execute(f'DROP OWNED BY "{role}"')
                c.execute(f'DROP ROLE IF EXISTS "{role}"')

        self.addCleanup(_cleanup)
        return role

    @contextlib.contextmanager
    def as_actor(
        self,
        conn: psycopg.Connection,
        app_role: str,
        actor_id: uuid.UUID | str | None,
        *,
        set_actor: bool = True,
    ):
        with conn.transaction():
            conn.execute(f'SET LOCAL ROLE "{app_role}"')
            if set_actor:
                val = "" if actor_id is None else str(actor_id)
                conn.execute("SELECT set_config('lab.actor_id', %s, true)", (val,))
            yield

    def _ensure_row(
        self,
        conn: psycopg.Connection,
        table: str,
        context: dict[str, Any],
        overrides: dict[str, Any] | None = None,
        type_hint: str | None = None,
    ) -> dict[str, Any]:
        """Insert a valid row into ``table`` by inspecting live catalog metadata and FKs."""
        seq = self._next_seq()
        row: dict[str, Any] = dict(overrides or {})

        cols = conn.execute(
            """
            SELECT
                a.attname AS col_name,
                t.typname AS type_name,
                t.typtype AS type_kind,
                t.oid AS type_oid,
                a.attnotnull AS not_null,
                pg_get_expr(d.adbin, d.adrelid) AS default_expr
            FROM pg_attribute a
            JOIN pg_type t ON t.oid = a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s::regclass
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (table,),
        ).fetchall()
        col_meta = {
            c[0]: {
                "type_name": c[1],
                "type_kind": c[2],
                "type_oid": c[3],
                "not_null": c[4],
                "default_expr": c[5],
            }
            for c in cols
        }

        fks = conn.execute(
            """
            SELECT
                c.confrelid::regclass::text AS ref_table,
                array_agg(a.attname ORDER BY u.ord) AS local_cols,
                array_agg(af.attname ORDER BY u.ord) AS ref_cols
            FROM pg_constraint c
            JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS u(attnum, refnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum
            JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = u.refnum
            WHERE c.conrelid = %s::regclass
              AND c.contype = 'f'
            GROUP BY c.oid, c.confrelid
            """,
            (table,),
        ).fetchall()

        for ref_table, local_cols, ref_cols in fks:
            clean_ref = ref_table.split(".")[-1].strip('"')
            if all(lc in row for lc in local_cols):
                continue
            if not any(col_meta[lc]["not_null"] for lc in local_cols):
                continue
            if clean_ref != "resource_versions" and all(
                (lc in row or lc in context) for lc in local_cols
            ):
                for lc in local_cols:
                    row.setdefault(lc, context[lc])
                continue
            child_hint = table.split("_")[0] if clean_ref == "resource_versions" else None
            parent = self._ensure_row(conn, clean_ref, context, type_hint=child_hint)
            for lc, rc in zip(local_cols, ref_cols):
                row.setdefault(lc, parent[rc])

        checks = [
            r[0]
            for r in conn.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = %s::regclass AND contype = 'c'
                """,
                (table,),
            ).fetchall()
        ]

        for cname, meta in col_meta.items():
            if cname in row:
                continue
            if cname == "id" and meta["type_name"] == "uuid":
                row[cname] = uuid.uuid4()
                continue
            if cname in context and meta["not_null"]:
                row[cname] = context[cname]
                continue
            if not meta["not_null"] or meta["default_expr"] is not None:
                continue

            tname = meta["type_name"]
            tkind = meta["type_kind"]

            if tkind == "e":
                labels = [
                    r[0]
                    for r in conn.execute(
                        "SELECT enumlabel FROM pg_enum WHERE enumtypid = %s ORDER BY enumsortorder",
                        (meta["type_oid"],),
                    ).fetchall()
                ]
                chosen = labels[0]
                if type_hint:
                    for lbl in labels:
                        if type_hint in lbl:
                            chosen = lbl
                            break
                row[cname] = chosen
            elif tname == "uuid":
                row[cname] = uuid.uuid4()
            elif tname in ("int2", "int4", "int8"):
                if cname in ("page_index", "ordinal", "rotation"):
                    row[cname] = seq - 1 if cname in ("page_index", "ordinal") else 0
                elif cname == "page_count":
                    row[cname] = 20
                else:
                    row[cname] = 1
            elif tname in ("float4", "float8", "numeric"):
                if "x0" in cname or "y0" in cname:
                    row[cname] = 0.1
                elif "x1" in cname or "y1" in cname:
                    row[cname] = 0.9
                else:
                    row[cname] = 1.0
            elif tname in ("_float4", "_float8", "_numeric"):
                row[cname] = [0.1, 0.1, 0.9, 0.9]
            elif tname == "bool":
                row[cname] = False
            elif tname in ("json", "jsonb"):
                row[cname] = Jsonb([0.1, 0.1, 0.9, 0.9] if "bbox" in cname else {})
            elif tname in ("timestamptz", "timestamp"):
                row[cname] = "2026-01-01T00:00:00Z"
            else:
                allowed: list[str] = []
                for chk in checks:
                    if re.search(rf"\b{re.escape(cname)}\b", chk):
                        allowed.extend(re.findall(r"'([^']*)'", chk))
                allowed = [a for a in allowed if not a.startswith("^")]
                if allowed:
                    chosen = allowed[0]
                    if type_hint:
                        for a in allowed:
                            if type_hint in a:
                                chosen = a
                                break
                    row[cname] = chosen
                elif "hash" in cname or "sha256" in cname:
                    row[cname] = h(f"{table}_{cname}_{seq}_{uuid.uuid4().hex}")
                elif "mime" in cname or "content_type" in cname or "media_type" in cname:
                    row[cname] = "application/pdf"
                else:
                    row[cname] = f"{cname}_{seq}_{uuid.uuid4().hex[:8]}"

        col_list = list(row.keys())
        cols_sql = ", ".join(f'"{c}"' for c in col_list)
        placeholders = ", ".join(["%s"] * len(col_list))
        cur = conn.execute(
            f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders}) RETURNING *',
            [row[c] for c in col_list],
        )
        out_cols = [d.name for d in cur.description]
        vals = cur.fetchone()
        return dict(zip(out_cols, vals))

    def seed_benchmark_gold(self, conn: psycopg.Connection, role: str = "owner") -> dict[str, Any]:
        """Create a project, document_version, benchmark_revision, question, and 4 gold rows."""
        project_id, owner_id = self.make_project(conn, role=role)
        ctx = {
            "project_id": project_id,
            "actor_id": owner_id,
            "created_by": owner_id,
        }
        with conn.transaction():
            conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(owner_id),))
            doc = self._ensure_row(conn, "document_versions", ctx, type_hint="doc")
            ctx["document_version_id"] = doc["id"]

            bench_res = self._ensure_row(conn, "resource_versions", ctx, type_hint="bench")
            bench_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO benchmark_revisions
                    (id, project_id, resource_id, document_version_id, n_target, q_min)
                VALUES (%s, %s, %s, %s, 20, 2)
                """,
                (bench_id, project_id, bench_res["id"], doc["id"]),
            )

            eu1 = self._ensure_row(conn, "evidence_units", ctx)
            eu2 = self._ensure_row(conn, "evidence_units", ctx)
            eu3 = self._ensure_row(conn, "evidence_units", ctx)

            q_id = uuid.uuid4()
            q_text = f"Question for benchmark {bench_id}?"
            conn.execute(
                """
                INSERT INTO questions
                    (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload)
                VALUES (%s, %s, 'q1', 0, %s, %s, 'general', %s)
                """,
                (q_id, bench_id, q_text, h(q_text), Jsonb({"question_id": "q1"})),
            )

            fact_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO facts
                    (id, benchmark_id, question_id, fact_key, statement, weight)
                VALUES (%s, %s, %s, 'f1', 'Primary required fact.', 1.0)
                """,
                (fact_id, bench_id, q_id),
            )

            eset_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO evidence_sets (id, benchmark_id, question_id, set_key)
                VALUES (%s, %s, %s, 'es1')
                """,
                (eset_id, bench_id, q_id),
            )

            conn.execute(
                """
                INSERT INTO evidence_set_members (evidence_set_id, benchmark_id, evidence_unit_id)
                VALUES (%s, %s, %s)
                """,
                (eset_id, bench_id, eu1["id"]),
            )

            conn.execute(
                """
                INSERT INTO distractors
                    (question_id, benchmark_id, evidence_unit_id, confusion_reason, disambiguating_condition)
                VALUES (%s, %s, %s, 'Same metric in preliminary table', 'Official high-load condition only')
                """,
                (q_id, bench_id, eu2["id"]),
            )

        return {
            "project_id": project_id,
            "owner_id": owner_id,
            "document_version_id": doc["id"],
            "benchmark_id": bench_id,
            "question_id": q_id,
            "fact_id": fact_id,
            "evidence_set_id": eset_id,
            "eu1_id": eu1["id"],
            "eu2_id": eu2["id"],
            "eu3_id": eu3["id"],
        }

    def _gold_counts(self, conn: psycopg.Connection, benchmark_id: uuid.UUID) -> dict[str, int]:
        out: dict[str, int] = {}
        for tbl in ("facts", "evidence_sets", "evidence_set_members", "distractors"):
            row = conn.execute(
                f"SELECT count(*) FROM {tbl} WHERE benchmark_id = %s",
                (benchmark_id,),
            ).fetchone()
            out[tbl] = int(row[0])
        return out

    def test_role_read_matrix_and_default_deny(self) -> None:
        """1. owner/maintainer/reviewer read gold; participant/observer/non-member/unset read 0 rows."""
        with self.connect() as conn:
            data = self.seed_benchmark_gold(conn)
            proj_id = data["project_id"]
            bench_id = data["benchmark_id"]
            owner_id = data["owner_id"]

            maintainer_id = self.add_member(conn, proj_id, "maintainer")
            reviewer_id = self.add_member(conn, proj_id, "reviewer")
            participant_id = self.add_member(conn, proj_id, "participant")
            observer_id = self.add_member(conn, proj_id, "observer")

            non_member_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')",
                    (non_member_id,),
                )

            app_role = self.make_app_role(conn)

            for allowed_actor in (owner_id, maintainer_id, reviewer_id):
                with self.as_actor(conn, app_role, allowed_actor):
                    counts = self._gold_counts(conn, bench_id)
                    self.assertEqual(
                        counts,
                        {
                            "facts": 1,
                            "evidence_sets": 1,
                            "evidence_set_members": 1,
                            "distractors": 1,
                        },
                    )

            for denied_actor in (participant_id, observer_id, non_member_id, None):
                with self.as_actor(conn, app_role, denied_actor):
                    counts = self._gold_counts(conn, bench_id)
                    self.assertEqual(
                        counts,
                        {
                            "facts": 0,
                            "evidence_sets": 0,
                            "evidence_set_members": 0,
                            "distractors": 0,
                        },
                    )

        # Also verify in a brand-new connection where lab.actor_id was never set at all
        with self.connect() as fresh_conn:
            with self.as_actor(fresh_conn, app_role, None, set_actor=False):
                counts = self._gold_counts(fresh_conn, bench_id)
                self.assertEqual(
                    counts,
                    {
                        "facts": 0,
                        "evidence_sets": 0,
                        "evidence_set_members": 0,
                        "distractors": 0,
                    },
                )

    def test_write_gold_reviewer_denied_maintainer_allowed(self) -> None:
        """2. reviewer cannot INSERT fact (RLS 42501 InsufficientPrivilege); maintainer can."""
        with self.connect() as conn:
            data = self.seed_benchmark_gold(conn)
            proj_id = data["project_id"]
            bench_id = data["benchmark_id"]
            q_id = data["question_id"]

            reviewer_id = self.add_member(conn, proj_id, "reviewer")
            maintainer_id = self.add_member(conn, proj_id, "maintainer")
            app_role = self.make_app_role(conn)

            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.as_actor(conn, app_role, reviewer_id):
                    conn.execute(
                        """
                        INSERT INTO facts
                            (id, benchmark_id, question_id, fact_key, statement, weight)
                        VALUES (%s, %s, %s, 'f_rev', 'Reviewer attempt', 1.0)
                        """,
                        (uuid.uuid4(), bench_id, q_id),
                    )

            new_fact_id = uuid.uuid4()
            with self.as_actor(conn, app_role, maintainer_id):
                conn.execute(
                    """
                    INSERT INTO facts
                        (id, benchmark_id, question_id, fact_key, statement, weight)
                    VALUES (%s, %s, %s, 'f_maint', 'Maintainer fact', 2.0)
                    """,
                    (new_fact_id, bench_id, q_id),
                )
                counts = self._gold_counts(conn, bench_id)
                self.assertEqual(counts["facts"], 2)

    def test_cross_project_isolation(self) -> None:
        """3. Project A owner cannot read or write Project B gold."""
        with self.connect() as conn:
            proj_a = self.seed_benchmark_gold(conn)
            proj_b = self.seed_benchmark_gold(conn)
            app_role = self.make_app_role(conn)

            with self.as_actor(conn, app_role, proj_a["owner_id"]):
                counts_b = self._gold_counts(conn, proj_b["benchmark_id"])
                self.assertEqual(
                    counts_b,
                    {
                        "facts": 0,
                        "evidence_sets": 0,
                        "evidence_set_members": 0,
                        "distractors": 0,
                    },
                )
                counts_a = self._gold_counts(conn, proj_a["benchmark_id"])
                self.assertEqual(
                    counts_a,
                    {
                        "facts": 1,
                        "evidence_sets": 1,
                        "evidence_set_members": 1,
                        "distractors": 1,
                    },
                )

            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.as_actor(conn, app_role, proj_a["owner_id"]):
                    conn.execute(
                        """
                        INSERT INTO facts
                            (id, benchmark_id, question_id, fact_key, statement, weight)
                        VALUES (%s, %s, %s, 'f_cross', 'Cross-project write attempt', 1.0)
                        """,
                        (uuid.uuid4(), proj_b["benchmark_id"], proj_b["question_id"]),
                    )

    def test_questions_readable_by_participant(self) -> None:
        """4. questions table is not blocked by gold RLS: participant can read questions."""
        with self.connect() as conn:
            data = self.seed_benchmark_gold(conn)
            participant_id = self.add_member(conn, data["project_id"], "participant")
            app_role = self.make_app_role(conn)

            with self.as_actor(conn, app_role, participant_id):
                q_rows = conn.execute(
                    "SELECT id, question_key, question_text FROM questions WHERE benchmark_id = %s",
                    (data["benchmark_id"],),
                ).fetchall()
                self.assertEqual(len(q_rows), 1)
                self.assertEqual(q_rows[0][0], data["question_id"])
                self.assertEqual(q_rows[0][1], "q1")

                # Gold tables remain hidden in the same transaction
                self.assertEqual(
                    self._gold_counts(conn, data["benchmark_id"]),
                    {
                        "facts": 0,
                        "evidence_sets": 0,
                        "evidence_set_members": 0,
                        "distractors": 0,
                    },
                )

    def test_trigger_path_as_app_role(self) -> None:
        """5. Trigger path (evidence_from_benchmark_document) succeeds when maintainer inserts evidence_set_members."""
        with self.connect() as conn:
            data = self.seed_benchmark_gold(conn)
            maintainer_id = self.add_member(conn, data["project_id"], "maintainer")
            app_role = self.make_app_role(conn)

            with self.as_actor(conn, app_role, maintainer_id):
                conn.execute(
                    """
                    INSERT INTO evidence_set_members (evidence_set_id, benchmark_id, evidence_unit_id)
                    VALUES (%s, %s, %s)
                    """,
                    (data["evidence_set_id"], data["benchmark_id"], data["eu3_id"]),
                )
                count_row = conn.execute(
                    "SELECT count(*) FROM evidence_set_members WHERE evidence_set_id = %s",
                    (data["evidence_set_id"],),
                ).fetchone()
                self.assertEqual(int(count_row[0]), 2)


if __name__ == "__main__":
    unittest.main()
