"""Real PostgreSQL tests for GET /api/v1/resources/{id}/lineage (SPEC 8, 10.2, 16.4)."""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "db", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_db import lineage as lineage_db
from lab_db.repo import Change, record_change

try:
    from tests.db.base import ADMIN_URL, FreshDatabase, h
except ImportError:  # pragma: no cover
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RealLineageEndpointTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": self.url,
            "LAB_ARTIFACT_ROOT": str(Path(self.tmp.name).resolve()),
            "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
            "LAB_DB_CONNECT_TIMEOUT_S": "5",
        }
        with patch.dict(os.environ, env, clear=True):
            self.settings = load_settings()
        self.client = TestClient(create_app(settings=self.settings, checks={}))
        self.secret = get_signing_secret(self.settings)

    def token(self, actor_id) -> dict:
        return {"Authorization": f"Bearer {issue_actor_token(actor_id, self.secret)}"}

    def resource(self, conn, project_id, actor_id, logical_id, lineage=None, revision=1):
        return record_change(conn, Change(
            project_id=project_id, actor_id=actor_id, operation_kind="test.lineage",
            resource_type="document", logical_id=logical_id, revision=revision,
            content_hash=h(f"document:{logical_id}:{revision}"), c14n_version="v1",
            audit_action="resource.created", lineage=lineage or [],
        ))

    def chain(self, conn, project_id, actor_id, length):
        """r0 <- r1 <- ... : each resource is derived from the previous one."""
        ids = []
        for i in range(length):
            lineage = [(ids[-1], "derived_from")] if ids else []
            ids.append(self.resource(conn, project_id, actor_id, f"r{i}", lineage=lineage))
        return ids

    def get(self, headers, rid, **params):
        return self.client.get(f"/api/v1/resources/{rid}/lineage", params=params, headers=headers)

    def test_upstream_chain_depth_and_truncation(self) -> None:
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 4)
        head = self.token(owner)

        full = self.get(head, ids[3])
        self.assertEqual(full.status_code, 200, full.text)
        body = full.json()
        self.assertEqual(body["project_id"], str(project_id))
        self.assertEqual({n["id"]: n["depth"] for n in body["nodes"]}, {str(ids[3]): 0, str(ids[2]): 1, str(ids[1]): 2, str(ids[0]): 3})
        self.assertEqual(
            sorted((e["upstream_id"], e["downstream_id"], e["relation"]) for e in body["edges"]),
            sorted((str(ids[i]), str(ids[i + 1]), "derived_from") for i in range(3)),
        )
        self.assertEqual((body["next_cursor"], body["truncated"], body["frontier_count"]), (None, False, 0))
        self.assertEqual(body["nodes"][0]["logical_id"], "r3")
        self.assertRegex(body["nodes"][0]["content_hash"], r"^[0-9a-f]{64}$")

        shallow = self.get(head, ids[3], depth=1).json()
        self.assertEqual(len(shallow["nodes"]), 2)
        self.assertEqual((shallow["truncated"], shallow["frontier_count"]), (True, 1))

        capped = self.get(head, ids[3], max_nodes=2).json()
        self.assertEqual(len(capped["nodes"]), 2)
        self.assertTrue(capped["truncated"])

    def test_downstream_direction(self) -> None:
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 4)
        body = self.get(self.token(owner), ids[0], direction="downstream").json()
        self.assertEqual({n["id"]: n["depth"] for n in body["nodes"]}, {str(ids[i]): i for i in range(4)})
        self.assertEqual(body["direction"], "downstream")
        up = self.get(self.token(owner), ids[0]).json()
        self.assertEqual([n["id"] for n in up["nodes"]], [str(ids[0])])

    def test_pagination_returns_every_node_and_edge_exactly_once(self) -> None:
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 6)
        head = self.token(owner)
        nodes, edges, cursor, pages = [], [], None, 0
        while True:
            params = {"depth": 10, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = self.get(head, ids[5], **params)
            self.assertEqual(page.status_code, 200, page.text)
            data = page.json()
            pages += 1
            nodes += [n["id"] for n in data["nodes"]]
            edges += [(e["upstream_id"], e["downstream_id"]) for e in data["edges"]]
            cursor = data["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10)
        self.assertEqual(pages, 3)
        self.assertEqual(nodes, [str(ids[i]) for i in (5, 4, 3, 2, 1, 0)])
        self.assertEqual(sorted(edges), sorted((str(ids[i]), str(ids[i + 1])) for i in range(5)))

        first = self.get(head, ids[5], depth=10, limit=2).json()["next_cursor"]
        for label, params in {
            "other direction": {"direction": "downstream"}, "other depth": {"depth": 9},
            "tampered": {"cursor": first[:-2] + "xx"},
        }.items():
            with self.subTest(label):
                merged = {"depth": 10, "limit": 2, "cursor": first, **params}
                res = self.get(head, ids[5], **merged)
                self.assertEqual((res.status_code, res.json()["error"]["code"]), (400, "bad_request"))
        # Another resource's cursor does not carry over either.
        res = self.get(head, ids[4], depth=10, limit=2, cursor=first)
        self.assertEqual(res.status_code, 400)

    def test_role_matrix_and_no_existence_leak(self) -> None:
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            people = {role: self.add_member(conn, project_id, role)
                      for role in ("maintainer", "reviewer", "participant", "observer")}
            _, outsider = self.make_project(conn, role="owner")
            other_project, other_owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 2)
            foreign = self.resource(conn, other_project, other_owner, "foreign")

        for role, actor in (("owner", owner), *people.items()):
            res = self.get(self.token(actor), ids[1])
            expected = 200 if role in ("owner", "maintainer", "reviewer") else 403
            with self.subTest(role=role):
                self.assertEqual(res.status_code, expected, res.text)
                if expected == 403:
                    self.assertEqual(res.json()["error"]["code"], "permission_denied")

        unknown = self.get(self.token(owner), uuid.uuid4())
        not_member = self.get(self.token(outsider), ids[1])
        other_project_resource = self.get(self.token(owner), foreign)
        self.assertEqual({r.status_code for r in (unknown, not_member, other_project_resource)}, {404})
        shapes = {(r.json()["error"]["code"], r.json()["error"]["message"]) for r in (unknown, not_member, other_project_resource)}
        self.assertEqual(shapes, {("not_found", "resource not found")}, "a missing resource and a forbidden one must look the same")

        self.assertEqual(self.client.get(f"/api/v1/resources/{ids[1]}/lineage").status_code, 401)

    def test_bad_parameters_are_400_before_any_data_is_read(self) -> None:
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 2)
        head = self.token(owner)
        for params in ({"depth": 0}, {"depth": 11}, {"direction": "sideways"}, {"limit": 0}, {"limit": 201},
                       {"max_nodes": 1001}, {"surprise": 1}, {"depth": "x"}):
            with self.subTest(params=params):
                res = self.get(head, ids[1], **params)
                self.assertEqual((res.status_code, res.json()["error"]["code"]), (400, "bad_request"))
        repeated = self.client.get(f"/api/v1/resources/{ids[1]}/lineage?depth=1&depth=2", headers=head)
        self.assertEqual(repeated.status_code, 400)

    def test_cursor_tampering_with_preserved_query_hash_rejected_400(self) -> None:
        """P2 regression: preserving q while altering depth or id returns 400 on real DB."""
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            ids = self.chain(conn, project_id, owner, 4)
        head = self.token(owner)

        # Get a legitimate signed cursor from page 1
        page1 = self.get(head, ids[3], depth=3, limit=1)
        self.assertEqual(page1.status_code, 200, page1.text)
        cursor1 = page1.json()["next_cursor"]
        self.assertIsNotNone(cursor1)

        # Tamper: decode legitimate cursor, keep q, modify d to 10 and id to non-existent UUID
        raw = base64.urlsafe_b64decode(cursor1 + "=" * (-len(cursor1) % 4))
        payload = json.loads(raw)
        payload["d"] = 10
        payload["id"] = str(uuid.uuid4())
        tampered_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        tampered_cursor = base64.urlsafe_b64encode(tampered_bytes).rstrip(b"=").decode("ascii")

        res = self.get(head, ids[3], depth=3, limit=1, cursor=tampered_cursor)
        self.assertEqual(res.status_code, 400, "Tampered cursor with preserved q must return 400")
        self.assertEqual(res.json()["error"]["code"], "bad_request")
        self.assertIn("cursor", res.json()["error"]["message"])

        # Also test: tampered signature returns 400
        payload["s"] = "0" * 32
        tampered_sig_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        tampered_sig_cursor = base64.urlsafe_b64encode(tampered_sig_bytes).rstrip(b"=").decode("ascii")
        res_sig = self.get(head, ids[3], depth=3, limit=1, cursor=tampered_sig_cursor)
        self.assertEqual(res_sig.status_code, 400)

    def test_high_fanout_resource_bounds_database_reads_and_frontier(self) -> None:
        """P1 regression: high fanout resource enforces bounded reads and frontier."""
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            root_id = self.resource(conn, project_id, owner, "root")
            # Create 60 downstream resources derived from root
            downstream_ids = []
            for i in range(60):
                d_id = self.resource(
                    conn, project_id, owner, f"child_{i}", lineage=[(root_id, "derived_from")]
                )
                downstream_ids.append(d_id)

        head = self.token(owner)

        # 1. max_nodes=1 & limit=1 on root downstream:
        # DB reads and frontier must be bounded; truncated must be True.
        res = self.get(head, root_id, direction="downstream", max_nodes=1, limit=1)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(len(body["nodes"]), 1)
        self.assertEqual(body["nodes"][0]["id"], str(root_id))
        self.assertEqual(body["edges"], [])
        self.assertTrue(body["truncated"])
        self.assertLessEqual(body["frontier_count"], lineage_db.MAX_FRONTIER)
        self.assertEqual(body["frontier_count"], 60)

        # 2. Direct walk with max_frontier bound:
        with self.connect() as conn:
            walk_res = lineage_db.walk(
                conn, project_id, root_id, direction="downstream", max_nodes=1, max_frontier=10
            )
            self.assertEqual(len(walk_res.frontier), 10)
            self.assertTrue(walk_res.truncated)

        # 3. Paging over high-fanout graph with limit=20
        pages = []
        cursor = None
        while True:
            params = {"direction": "downstream", "depth": 1, "limit": 20}
            if cursor:
                params["cursor"] = cursor
            p = self.get(head, root_id, **params)
            self.assertEqual(p.status_code, 200)
            p_data = p.json()
            pages.append(p_data)
            cursor = p_data["next_cursor"]
            if cursor is None:
                break
        total_nodes = [n["id"] for page in pages for n in page["nodes"]]
        self.assertEqual(len(total_nodes), 61)  # root + 60 children
        self.assertEqual(len(pages), 4)  # ceil(61/20) = 4

    def test_multi_operation_duplicate_edges_bounded(self) -> None:
        """P1 regression: multi-operation duplicate edges between endpoints are strictly bounded."""
        with self.connect() as conn:
            project_id, owner = self.make_project(conn, role="owner")
            parent_id = self.resource(conn, project_id, owner, "parent")
            child_id = self.resource(
                conn, project_id, owner, "child", lineage=[(parent_id, "derived_from")]
            )
            # Insert 50 additional operations and lineage_edges between parent and child
            with conn.transaction():
                for i in range(50):
                    op_id = uuid.uuid4()
                    conn.execute(
                        "INSERT INTO operations (id, project_id, kind, actor_id, status) "
                        "VALUES (%s, %s, %s, %s, 'succeeded')",
                        (op_id, project_id, f"test.multi_op_{i}", owner),
                    )
                    conn.execute(
                        "INSERT INTO lineage_edges (project_id, upstream_id, downstream_id, relation, operation_id) "
                        "VALUES (%s, %s, %s, 'derived_from', %s)",
                        (project_id, parent_id, child_id, op_id),
                    )

        head = self.token(owner)

        # 1. Direct walk with max_edges=10 bounds edges read and returned
        with self.connect() as conn:
            walk_res = lineage_db.walk(
                conn, project_id, child_id, direction="upstream", max_edges=10
            )
            self.assertEqual(len(walk_res.edges), 10)
            self.assertTrue(walk_res.truncated)

        # 2. API query with max_edges=10 bounds response edges
        res = self.get(head, child_id, direction="upstream", max_edges=10)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(len(body["edges"]), 10)
        self.assertTrue(body["truncated"])

        # 3. API query with max_edges=25
        res25 = self.get(head, child_id, direction="upstream", max_edges=25)
        self.assertEqual(res25.status_code, 200)
        body25 = res25.json()
        self.assertEqual(len(body25["edges"]), 25)
        self.assertTrue(body25["truncated"])
