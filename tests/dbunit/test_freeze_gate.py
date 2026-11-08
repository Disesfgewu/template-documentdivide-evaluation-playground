"""Unit tests for fail-closed benchmark freeze gate (SPEC 10, R05, R06, issue #10).

Verifies the optimistic-concurrency state change boundary in lab_db.repo
and its simulation of the 0010 database trigger boundary without requiring a live
PostgreSQL instance. Real PostgreSQL tests reside in tests/db/test_benchmarks.py.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import uuid

from psycopg import errors

from lab_db.repo import (
    FreezeIneligible,
    NotFound,
    PermissionDenied,
    StaleVersion,
    transition_benchmark,
)


class MockConnection:
    """Mock database connection simulating benchmark state transitions and DB guard triggers."""

    def __init__(
        self,
        role: str = "owner",
        from_status: str = "ready_to_freeze",
        row_version: int = 6,
        current_compliance: str | None = None,
        resource_id: uuid.UUID | None = None,
        user: str = "lab_admin",
        allow_legacy_freeze: bool = False,
    ) -> None:
        self.role = role
        self.from_status = from_status
        self.row_version = row_version
        self.current_compliance = current_compliance
        self.resource_id = resource_id or uuid.uuid4()
        self.user = user
        self.allow_legacy_freeze = allow_legacy_freeze
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, query: str, params: tuple | None = None) -> MagicMock:
        self.executed.append((query, params))
        result = MagicMock()
        if "set_config('lab.test_allow_legacy_freeze'" in query:
            self.allow_legacy_freeze = True
        elif "FROM project_members" in query:
            result.fetchone.return_value = (self.role,)
        elif "FROM benchmark_revisions" in query and "FOR UPDATE" in query:
            result.fetchone.return_value = (
                self.from_status,
                self.row_version,
                self.resource_id,
                self.current_compliance,
            )
        elif "UPDATE benchmark_revisions" in query:
            to_status = params[0] if params else None
            if to_status == "frozen":
                if self.allow_legacy_freeze:
                    if self.user.startswith("lab_app_"):
                        raise errors.InsufficientPrivilege("app role cannot bypass benchmark freeze gate")
                else:
                    raise errors.CheckViolation(
                        f"cannot freeze benchmark revision {self.resource_id}: "
                        "mandatory eligibility verification is not implemented in current schema"
                    )
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
                "attempt freeze compliant",
                self.actor_id,
                compliance="compliant",
            )
        self.assertIn("mandatory eligibility verification is not implemented", str(ctx.exception))

    def test_freeze_as_partial_fails_closed_ineligible(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(FreezeIneligible) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze partial",
                self.actor_id,
                compliance="partial",
            )
        self.assertIn("mandatory eligibility verification is not implemented", str(ctx.exception))

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
        self.assertIn("mandatory eligibility verification is not implemented", str(ctx.exception))

    def test_freeze_with_existing_partial_row_fails_closed(self) -> None:
        conn = MockConnection(
            role="owner",
            from_status="ready_to_freeze",
            row_version=6,
            current_compliance="partial",
        )
        with self.assertRaises(FreezeIneligible) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "attempt freeze inherit partial",
                self.actor_id,
                compliance=None,
            )
        self.assertIn("mandatory eligibility verification is not implemented", str(ctx.exception))

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

    def test_freeze_by_reviewer_raises_permission_denied(self) -> None:
        conn = MockConnection(role="reviewer", from_status="ready_to_freeze", row_version=6)
        with self.assertRaises(PermissionDenied) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "reviewer freeze attempt",
                self.actor_id,
                compliance="partial",
            )
        self.assertIn("may only comment", str(ctx.exception))

    def test_freeze_legacy_test_bypass_succeeds(self) -> None:
        conn = MockConnection(role="owner", from_status="ready_to_freeze", row_version=6, allow_legacy_freeze=True)
        new_version = transition_benchmark(
            conn,
            self.project_id,
            self.benchmark_id,
            6,
            "frozen",
            "legacy test fixture freeze",
            self.actor_id,
            compliance="partial",
        )
        self.assertEqual(new_version, 7)
        # Verify transition audit and outbox were issued
        stmt_prefixes = [q.strip().split()[0] for q, _ in conn.executed]
        self.assertIn("UPDATE", stmt_prefixes)
        insert_stmts = [q for q, _ in conn.executed if q.strip().startswith("INSERT INTO")]
        self.assertEqual(len(insert_stmts), 3)  # operations, audit_events, outbox_events

    def test_freeze_legacy_test_bypass_blocked_for_app_role(self) -> None:
        conn = MockConnection(
            role="owner",
            from_status="ready_to_freeze",
            row_version=6,
            user="lab_app_worker",
            allow_legacy_freeze=True,
        )
        with self.assertRaises(errors.InsufficientPrivilege) as ctx:
            transition_benchmark(
                conn,
                self.project_id,
                self.benchmark_id,
                6,
                "frozen",
                "app bypass attempt",
                self.actor_id,
                compliance="partial",
            )
        self.assertIn("app role cannot bypass benchmark freeze gate", str(ctx.exception))

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


if __name__ == "__main__":
    unittest.main()
