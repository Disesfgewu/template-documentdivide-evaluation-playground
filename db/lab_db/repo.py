"""Data-access primitives on the 0001 core schema.

- ``authorize``: SPEC 16.4 role matrix, default deny, project-scoped.
- ``claim_idempotency``: one logical operation per (project, scope, key);
  same key with a different request hash is a conflict (409).
- ``record_change``: business row + lineage + audit + outbox in ONE
  transaction; if any part fails (e.g. the audit insert) everything rolls
  back (SPEC 10.2, T49).
- ``grant_app_privileges``: the application role gets DML only; it cannot
  alter schema, and audit/frozen guards are triggers it cannot bypass.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

import psycopg
from psycopg import errors, sql
from psycopg.types.json import Jsonb

Role = Literal["owner", "maintainer", "reviewer", "participant", "observer"]
ROLES: tuple[Role, ...] = ("owner", "maintainer", "reviewer", "participant", "observer")

# SPEC 16.4. Value per role: True = allowed, False = denied, or the name of
# the extra condition the caller must still verify for that role.
ROLE_MATRIX: dict[str, dict[Role, object]] = {
    "import_and_configure_parse": {"owner": True, "maintainer": True, "reviewer": False, "participant": False, "observer": False},
    "read_licensed_pdf_and_public_questions": {"owner": True, "maintainer": True, "reviewer": True, "participant": True, "observer": "explicit_grant"},
    "build_edit_freeze": {"owner": True, "maintainer": True, "reviewer": "review_comments_only", "participant": False, "observer": False},
    "read_gold_and_generation_context": {"owner": True, "maintainer": True, "reviewer": True, "participant": False, "observer": False},
    "register_method_and_submit": {"owner": True, "maintainer": True, "reviewer": False, "participant": "own_method_or_run", "observer": False},
    "read_run_results": {"owner": True, "maintainer": True, "reviewer": True, "participant": "own_run_per_feedback_profile", "observer": "published_aggregate"},
    "read_gold_trace_and_lineage": {"owner": True, "maintainer": True, "reviewer": True, "participant": False, "observer": False},
    "read_run_trace": {"owner": True, "maintainer": True, "reviewer": True, "participant": "own_run_hidden_content_removed", "observer": False},
    "delete_or_change_membership": {"owner": True, "maintainer": False, "reviewer": False, "participant": False, "observer": False},
}


class NotFound(Exception):
    """Unknown resource, or one in a project the actor may not see (404)."""


class PermissionDenied(Exception):
    """Visible resource, but the role may not perform the action (403)."""


class IdempotencyConflict(Exception):
    """Same idempotency key, different request (409)."""


@dataclass(frozen=True)
class Decision:
    role: Role
    condition: Optional[str]  # None = unconditional; otherwise the caller must check it


def authorize(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, action: str) -> Decision:
    if action not in ROLE_MATRIX:
        raise ValueError(f"unknown action {action!r}")
    row = conn.execute("SELECT role FROM project_members WHERE project_id = %s AND actor_id = %s",
                       (project_id, actor_id)).fetchone()
    if row is None:
        raise NotFound("project")  # never reveal that the project exists
    role = row[0]
    rule = ROLE_MATRIX[action][role]
    if rule is False:
        raise PermissionDenied(f"{role} may not {action}")
    return Decision(role=role, condition=None if rule is True else str(rule))


@dataclass(frozen=True)
class Claim:
    created: bool          # True: this call owns the new logical operation
    operation_id: uuid.UUID


def claim_idempotency(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID,
                      scope: str, key: str, request_hash: str, kind: str) -> Claim:
    """Concurrency-safe: under 20 simultaneous identical requests exactly one
    gets created=True; all others get the same operation_id (T48)."""
    op_id = uuid.uuid4()
    with conn.transaction():
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status) "
                     "VALUES (%s, %s, %s, %s, 'intent')", (op_id, project_id, kind, actor_id))
        row = conn.execute(
            "INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING operation_id",
            (project_id, scope, key, request_hash, op_id)).fetchone()
        if row is not None:
            return Claim(True, op_id)
        # Lost the race (or a replay): discard our operation row by rolling back.
        raise _Replay()


class _Replay(Exception):
    pass


def claim_or_replay(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID,
                    scope: str, key: str, request_hash: str, kind: str) -> Claim:
    try:
        return claim_idempotency(conn, project_id, actor_id, scope, key, request_hash, kind)
    except _Replay:
        pass
    existing_hash, existing_op = conn.execute(
        "SELECT request_hash, operation_id FROM idempotency_keys "
        "WHERE project_id = %s AND idempotency_scope = %s AND idempotency_key = %s",
        (project_id, scope, key)).fetchone()
    if existing_hash != request_hash:
        raise IdempotencyConflict(f"{scope}/{key} already used with a different request")
    return Claim(False, existing_op)


@dataclass
class Change:
    project_id: uuid.UUID
    actor_id: uuid.UUID
    operation_kind: str
    resource_type: str
    logical_id: str
    revision: int
    content_hash: str
    c14n_version: str
    audit_action: str
    reason: Optional[str] = None
    lineage: list[tuple[uuid.UUID, str]] = field(default_factory=list)  # (upstream resource id, relation)
    outbox_topic: Optional[str] = None
    outbox_payload: Optional[dict] = None
    trace_id: Optional[str] = None
    operation_id: Optional[uuid.UUID] = None


def record_change(conn: psycopg.Connection, ch: Change) -> uuid.UUID:
    """Create a new resource version with its lineage, audit event and outbox
    event atomically. Returns the new resource_versions.id."""
    rv_id = uuid.uuid4()
    op_id = ch.operation_id or uuid.uuid4()
    with conn.transaction():
        if ch.operation_id is not None:
            conn.execute(
                "UPDATE operations SET status = 'succeeded', finished_at = now() WHERE id = %s",
                (op_id,),
            )
        else:
            conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, trace_id, status, finished_at) "
                         "VALUES (%s, %s, %s, %s, %s, 'succeeded', now())",
                         (op_id, ch.project_id, ch.operation_kind, ch.actor_id, ch.trace_id))
        conn.execute("INSERT INTO resource_versions (id, project_id, resource_type, logical_id, revision, "
                     "content_hash, c14n_version, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (rv_id, ch.project_id, ch.resource_type, ch.logical_id, ch.revision,
                      ch.content_hash, ch.c14n_version, ch.actor_id))
        for upstream, relation in ch.lineage:
            conn.execute("INSERT INTO lineage_edges (project_id, upstream_id, downstream_id, relation, operation_id) "
                         "VALUES (%s, %s, %s, %s, %s)", (ch.project_id, upstream, rv_id, relation, op_id))
        conn.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, "
                     "before_revision, after_revision, reason, outcome, operation_id, trace_id) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', %s, %s)",
                     (uuid.uuid4(), ch.project_id, ch.actor_id, ch.audit_action, ch.resource_type, rv_id,
                      ch.revision - 1 if ch.revision > 1 else None, ch.revision, ch.reason, op_id, ch.trace_id))
        if ch.outbox_topic is not None:
            conn.execute("INSERT INTO outbox_events (event_id, project_id, topic, payload, operation_id) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (uuid.uuid4(), ch.project_id, ch.outbox_topic, Jsonb(ch.outbox_payload or {}), op_id))
    return rv_id


APPEND_ONLY_TABLES = ("audit_events", "operation_results", "benchmark_transitions", "job_transitions", "graph_checkpoints",
                      "log_events", "submissions", "retrieved_hits", "citations",
                      "visual_observations", "evidence_verifications",
                      "embedding_spaces", "vector_items", "vector_item_regions", "vector_index_revisions",
                      "vector_queries", "parse_jobs", "parse_results", "parse_page_results", "generate_jobs", "llm_calls", "gold_answers", "question_validations",
                      "evidence_unit_sources", "benchmark_parse_sources")
READ_ONLY_TABLES = ("schema_migrations", "benchmark_transitions_allowed", "job_transitions_allowed")


def grant_app_privileges(conn: psycopg.Connection, role: str) -> None:
    """Run by the migration/owner role. The app role gets DML only (no DDL,
    no TRUNCATE); append-only tables are INSERT/SELECT; bookkeeping tables
    are read-only. Triggers still guard frozen and audit rows for every role."""
    ident = sql.Identifier(role)
    tables = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")]
    with conn.transaction():
        conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident))
        for t in tables:
            if t in READ_ONLY_TABLES:
                privs = "SELECT"
            elif t in APPEND_ONLY_TABLES:
                privs = "SELECT, INSERT"
            else:
                privs = "SELECT, INSERT, UPDATE"
            conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(sql.SQL(privs), sql.Identifier(t), ident))
        conn.execute(sql.SQL("GRANT DELETE ON artifacts TO {}").format(ident))  # staged clean-up only (trigger-guarded)
        conn.execute(sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident))


class StaleVersion(Exception):
    """row_version did not match: someone else changed the row (409)."""


class FreezeIneligible(errors.CheckViolation):
    """The benchmark revision cannot be frozen (no passing automated validation for every question, no questions, or
    a compliance other than 'partial'); the database gate (0017) is the authority, this is how it surfaces."""


def transition_benchmark(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID,
                         expected_row_version: int, to_status: str, reason: str, actor_id: uuid.UUID,
                         compliance: Optional[str] = None) -> int:
    """Optimistic-concurrency state change of a benchmark revision, scoped to
    ``project_id`` and authorized for ``actor_id`` (build_edit_freeze; a
    reviewer's "comments only" right does not allow it). The DB rejects
    illegal transitions and writes the transition row itself (0007 trigger);
    this function adds the operation, audit and outbox event in the same
    transaction. Returns the new row_version."""
    if not reason or not reason.strip():
        raise ValueError("a transition reason is required")
    decision = authorize(conn, project_id, actor_id, "build_edit_freeze")
    if decision.condition is not None:
        raise PermissionDenied(f"{decision.role} may only comment, not {to_status} a benchmark")
    with conn.transaction():
        cur = conn.execute("SELECT status::text, row_version, resource_id, compliance FROM benchmark_revisions "
                           "WHERE id = %s AND project_id = %s FOR UPDATE", (benchmark_id, project_id)).fetchone()
        if cur is None:
            raise NotFound("benchmark")
        from_status, version, resource_id, current_compliance = cur
        if version != expected_row_version:
            raise StaleVersion(f"benchmark {benchmark_id} is at row_version {version}, not {expected_row_version}")
        effective_compliance = compliance if compliance is not None else current_compliance
        if to_status == "frozen":
            if not effective_compliance:
                raise ValueError("a compliance designation ('partial') is required to freeze a benchmark")
            if effective_compliance not in ("compliant", "partial"):
                raise ValueError(f"invalid compliance {effective_compliance!r}")
            if decision.role != "owner":
                raise PermissionDenied(f"{decision.role} may not freeze a benchmark: owner required")
        conn.execute("SELECT set_config('lab.actor_id', %s, true), set_config('lab.transition_reason', %s, true)",
                     (str(actor_id), reason))
        try:
            new_version = conn.execute(
                "UPDATE benchmark_revisions SET status = %s, row_version = row_version + 1, "
                "compliance = COALESCE(%s, compliance), "
                "frozen_at = CASE WHEN %s::text = 'frozen' THEN now() ELSE frozen_at END "
                "WHERE id = %s RETURNING row_version",
                (to_status, compliance, to_status, benchmark_id)).fetchone()[0]
        except errors.CheckViolation as exc:
            if "cannot freeze benchmark revision" in str(exc):
                raise FreezeIneligible(str(exc)) from exc
            raise
        op_id = uuid.uuid4()
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'benchmark.transition', %s, 'succeeded', now())", (op_id, project_id, actor_id))
        conn.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, "
                     "before_revision, after_revision, reason, outcome, operation_id) "
                     "VALUES (%s, %s, %s, %s, 'benchmark_revision', %s, %s, %s, %s, 'succeeded', %s)",
                     (uuid.uuid4(), project_id, actor_id, f"benchmark.{to_status}", resource_id,
                      version, new_version, reason, op_id))
        conn.execute("INSERT INTO outbox_events (event_id, project_id, topic, payload, operation_id) "
                     "VALUES (%s, %s, 'benchmark.transitioned', %s, %s)",
                     (uuid.uuid4(), project_id,
                      Jsonb({"benchmark_id": str(benchmark_id), "from": from_status, "to": to_status,
                             "row_version": new_version}), op_id))
    return new_version
