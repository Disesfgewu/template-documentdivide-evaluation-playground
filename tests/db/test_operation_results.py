"""Real PostgreSQL tests for 0009_operation_results.sql: the saved original
response of an operation is immutable, well-formed and bound to its project."""
import json
import uuid

from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    IntegrityConstraintViolation,
    UniqueViolation,
)
from psycopg.types.json import Jsonb

from lab_db.repo import APPEND_ONLY_TABLES
from tests.db.base import FreshDatabase

BODY = {
    "code": "unsupported_encryption",
    "message": "document rejected: encrypted_pdf",
    "retryable": False,
    "details": {"reason": "encrypted_pdf", "sha256": "ab" * 32, "size_bytes": 12, "page_count": None},
    "retry_after": None,
}


class TestOperationResults(FreshDatabase):
    def make_operation(self, conn, project_id, actor_id, status="failed"):
        op_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                "INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'document.import', %s, %s)",
                (op_id, project_id, actor_id, status),
            )
        return op_id

    def insert(self, conn, project_id, op_id, status=422, body=BODY):
        with conn.transaction():
            conn.execute(
                "INSERT INTO operation_results (project_id, operation_id, http_status, response_body) VALUES (%s, %s, %s, %s)",
                (project_id, op_id, status, Jsonb(body)),
            )

    def test_round_trip_keeps_the_exact_object(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            op = self.make_operation(conn, p, a)
            self.insert(conn, p, op)
            row = conn.execute(
                "SELECT http_status, response_body FROM operation_results WHERE project_id = %s AND operation_id = %s",
                (p, op),
            ).fetchone()
        self.assertEqual(row[0], 422)
        body = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        self.assertEqual(body, BODY)

    def test_saved_response_is_immutable(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            op = self.make_operation(conn, p, a)
            self.insert(conn, p, op)
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE operation_results SET http_status = 500 WHERE operation_id = %s", (op,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE operation_results SET response_body = '{}'::jsonb WHERE operation_id = %s", (op,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM operation_results WHERE operation_id = %s", (op,))
            self.assertEqual(
                conn.execute("SELECT count(*) FROM operation_results WHERE operation_id = %s", (op,)).fetchone()[0], 1
            )

    def test_one_result_per_operation(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            op = self.make_operation(conn, p, a)
            self.insert(conn, p, op)
            with self.assertRaises(UniqueViolation):
                self.insert(conn, p, op, status=409)

    def test_status_must_be_an_http_response_status_and_body_an_object(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            for status in (200, 201, 422, 599):  # successes and rejections are both stored
                op = self.make_operation(conn, p, a)
                with self.subTest(valid=status):
                    self.insert(conn, p, op, status=status)
            for status in (0, 100, 199, 600, 999):
                op = self.make_operation(conn, p, a)
                with self.subTest(status=status), self.assertRaises(CheckViolation):
                    self.insert(conn, p, op, status=status)
            for label, body in (("array", [1]), ("string", "x"), ("null", None), ("number", 3)):
                op = self.make_operation(conn, p, a)
                with self.subTest(body=label), self.assertRaises(CheckViolation):
                    self.insert(conn, p, op, body=body)

    def test_result_is_bound_to_an_operation_of_the_same_project(self):
        with self.connect() as conn:
            p1, a1 = self.make_project(conn)
            p2, _ = self.make_project(conn)
            op1 = self.make_operation(conn, p1, a1)
            with self.assertRaises(ForeignKeyViolation):
                self.insert(conn, p2, op1)  # another project's operation
            with self.assertRaises(ForeignKeyViolation):
                self.insert(conn, p1, uuid.uuid4())  # no such operation

    def test_table_is_registered_append_only_for_the_app_role(self):
        """grant_app_privileges gives append-only tables SELECT and INSERT only;
        the restricted-role API tests in tests/api_db exercise the real grant."""
        self.assertIn("operation_results", APPEND_ONLY_TABLES)
