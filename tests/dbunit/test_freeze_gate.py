"""Unit tests for fail-closed benchmark freeze gate (SPEC 10, R05, R06, issue #10).

Verifies the optimistic-concurrency state change boundary in lab_db.repo
without requiring a live PostgreSQL instance. Real PostgreSQL tests reside
in tests/db/test_benchmarks.py.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import uuid

from lab_db.repo import (
    FreezeIneligible,
    NotFound,
    PermissionDenied,
    StaleVersion,
    transition_benchmark,
)


class MockConnection:
    """Mock database connection simulating benchmark state transitions."""

    def __init__(
        self,
        role: str = "owner",
        from_status: str = "ready_to_freeze",
        row_version: int = 6,
        current_compliance: str | None = None,
        resource_id: uuid.UUID | None = None,
    ) -> None:
        self.role = role
        self.from_status = from_status
        self.row_version = row_version
        self.current_compliance = current_compliance
        self.resource_id = resource_id or uuid.uuid4()
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, query: str, params: tuple | None = None) -> MagicMock:
        self.executed.append((query, params))
        result = MagicMock()
        if "FROM project_members" in query:
            result.fetchone.return_value = (self.role,)
        elif "FROM benchmark_revisions" in query and "FOR UPDATE" in query:
            result.fetchone.return_value = (
                self.from_status,
                self.row_version,
                self.resource_id,
                self.current_compliance,
            )
        elif "UPDATE benchmark_revisions" in query:
            result.fetchone.return_value = (self.row_version + 1,)
        else:
            result.fetchone.return_value = None
        return result

    def transaction(self):
        class TransactionContext:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        return TransactionContext()


class BenchmarkFreezeGateUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_id = uuid.uuid4()
        self.benchmark_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()

    def test_freeze_as_compliant_fails_closed_ineligible(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(FreezeIneligible) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze",
                self.actor_id,
                compliance="compliant",
            )
        self.assertIn("cannot freeze as compliant", str(ctx.exception))
        self.assertIn("mandatory eligibility", str(ctx.exception))
        # Ensure UPDATE benchmark_revisions was never executed
        update_calls = [q for q, _ in conn.executed if "UPDATE benchmark_revisions" in query_prefix(q)]
        self.assertEqual(update_calls, [])

    def test_freeze_with_existing_compliant_row_fails_closed(self) -> None:
        conn = MockConnection(
            role="owner",
            from_status="ready_to_freeze",
            row_version=6,
            current_compliance="compliant",
        )
        with self.assertRaises(FreezeIneligible) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze inherit compliant",
                self.actor_id,
                compliance=None,
            )
        self.assertIn("cannot freeze as compliant", str(ctx.exception))

    def test_freeze_missing_compliance_raises_value_error(self) -> None:
        conn = MockConnection(
            role="owner",
            from_status="ready_to_freeze",
            row_version=6,
            current_compliance=None,
        )
        with self.assertRaises(ValueError) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze missing compliance",
                self.actor_id,
                compliance=None,
            )
        self.assertIn("compliance designation", str(ctx.exception))

    def test_freeze_invalid_compliance_string_raises_value_error(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(ValueError) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze invalid compliance",
                self.actor_id,
                compliance="unknown_status",
            )
        self.assertIn("invalid compliance", str(ctx.exception))

    def test_freeze_partial_by_maintainer_raises_permission_denied(self) -> None:
        conn = MockConnection(role="maintainer", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(PermissionDenied) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "maintainer partial freeze",
                self.actor_id,
                compliance="partial",
            )
        self.assertIn("owner required", str(ctx.exception))

    def test_freeze_partial_by_reviewer_raises_permission_denied(self) -> None:
        conn = MockConnection(role="reviewer", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(PermissionDenied) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "reviewer partial freeze",
                self.actor_id,
                compliance="partial",
            )
        self.assertIn("may only comment", str(ctx.exception))

    def test_freeze_partial_by_owner_succeeds(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6)
        new_version = transition_benchmark(
            conn,
            self.project_id,
            self.benchmark_id,
            6,
            "frozen",
            "owner partial freeze",
            self.actor_id,
            compliance="partial",
        )
        self.assertEqual(new_version, 7)
        # Verify transition audit and outbox were issued
        stmt_prefixes = [q.strip().split()[0] for q, _ in conn.executed]
        self.assertIn("UPDATE", stmt_prefixes)
        insert_stmts = [q for q, _ in conn.executed if q.strip().startswith("INSERT INTO")]
        self.assertEqual(len(insert_stmts), 3)  # operations, audit_events, outbox_events

    def test_non_frozen_transitions_unaffected(self) -> None:
        conn = MockConnection(role="maintainer", from_status="planning", row_version=2)
        new_version = transition_benchmark(
            conn,
            self.project_id,
            self.benchmark_id,
            2,
            "generating",
            "advance to generating",
            self.actor_id,
        )
        self.assertEqual(new_version, 3)

    def test_stale_row_version_rejected(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(StaleVersion):
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                5,  # expected 5 but row is at 6
                "frozen",
                "stale attempt",
                self.actor_id,
                compliance="partial",
            )


def query_prefix(q: str) -> str:
    return q.strip().split()[0] if q.strip() else ""


if __name__ == "__main__":
    unittest.main()
