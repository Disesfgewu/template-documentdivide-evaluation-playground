"""Integration tests for bounded lineage traversal (SPEC 10.2, 16.4)."""

import unittest
import uuid

import psycopg

from lab_db.lineage import EdgeInfo, LineageResult, NodeInfo, walk
from lab_db.repo import Change, record_change

from .base import FreshDatabase, h


class LineageWalkTests(FreshDatabase):
    """Verify upstream/downstream lineage traversal, deduplication, cycles, and bounds."""

    def _create_resource(
        self,
        conn: psycopg.Connection,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        logical_id: str,
        revision: int = 1,
        resource_type: str = "document",
        lineage: list[tuple[uuid.UUID, str]] | None = None,
    ) -> uuid.UUID:
        """Create a resource version via record_change and return its UUID."""
        ch = Change(
            project_id=project_id,
            actor_id=actor_id,
            operation_kind="test.lineage",
            resource_type=resource_type,
            logical_id=logical_id,
            revision=revision,
            content_hash=h(f"{resource_type}:{logical_id}:{revision}"),
            c14n_version="v1",
            audit_action="resource.created",
            lineage=lineage or [],
        )
        return record_change(conn, ch)

    def test_chain_upstream_depth_truncation_and_full_walk(self) -> None:
        """Chain A<-B<-C<-D from D upstream respects max_depth=2 truncation and max_depth=3 completion."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            a_id = self._create_resource(conn, project_id, actor_id, "res_a")
            b_id = self._create_resource(
                conn, project_id, actor_id, "res_b", lineage=[(a_id, "derived_from")]
            )
            c_id = self._create_resource(
                conn, project_id, actor_id, "res_c", lineage=[(b_id, "derived_from")]
            )
            d_id = self._create_resource(
                conn, project_id, actor_id, "res_d", lineage=[(c_id, "derived_from")]
            )

            res_depth2 = walk(conn, project_id, d_id, direction="upstream", max_depth=2)
            self.assertIsInstance(res_depth2, LineageResult)
            self.assertEqual(set(res_depth2.nodes.keys()), {d_id, c_id, b_id})
            self.assertNotIn(a_id, res_depth2.nodes)
            self.assertEqual(res_depth2.nodes[d_id].depth, 0)
            self.assertEqual(res_depth2.nodes[c_id].depth, 1)
            self.assertEqual(res_depth2.nodes[b_id].depth, 2)
            self.assertEqual(res_depth2.nodes[b_id].logical_id, "res_b")
            self.assertEqual(res_depth2.nodes[b_id].resource_type, "document")
            self.assertEqual(res_depth2.nodes[b_id].revision, 1)
            self.assertEqual(res_depth2.nodes[b_id].content_hash, h("document:res_b:1"))
            self.assertEqual(
                [(e.upstream_id, e.downstream_id, e.relation) for e in res_depth2.edges],
                [
                    (c_id, d_id, "derived_from"),
                    (b_id, c_id, "derived_from"),
                ],
            )
            self.assertTrue(res_depth2.truncated)
            self.assertEqual(res_depth2.frontier, [a_id])

            res_depth3 = walk(conn, project_id, d_id, direction="upstream", max_depth=3)
            self.assertEqual(set(res_depth3.nodes.keys()), {d_id, c_id, b_id, a_id})
            self.assertEqual(res_depth3.nodes[a_id].depth, 3)
            self.assertEqual(len(res_depth3.edges), 3)
            self.assertFalse(res_depth3.truncated)
            self.assertEqual(res_depth3.frontier, [])

    def test_diamond_dag_deduplicates_node_and_preserves_all_edges(self) -> None:
        """Diamond D<-(B,C)<-A visits A once with depth=2 and collects all 4 edges."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            a_id = self._create_resource(conn, project_id, actor_id, "diamond_a")
            b_id = self._create_resource(
                conn, project_id, actor_id, "diamond_b", lineage=[(a_id, "extracted_from")]
            )
            c_id = self._create_resource(
                conn, project_id, actor_id, "diamond_c", lineage=[(a_id, "generated_from")]
            )
            d_id = self._create_resource(
                conn,
                project_id,
                actor_id,
                "diamond_d",
                lineage=[(b_id, "derived_from"), (c_id, "derived_from")],
            )

            res = walk(conn, project_id, d_id, direction="upstream", max_depth=3)
            self.assertEqual(len(res.nodes), 4)
            self.assertEqual(res.nodes[d_id].depth, 0)
            self.assertEqual(res.nodes[b_id].depth, 1)
            self.assertEqual(res.nodes[c_id].depth, 1)
            self.assertEqual(res.nodes[a_id].depth, 2)
            self.assertEqual(len(res.edges), 4)
            self.assertEqual(
                {(e.upstream_id, e.downstream_id, e.relation) for e in res.edges},
                {
                    (b_id, d_id, "derived_from"),
                    (c_id, d_id, "derived_from"),
                    (a_id, b_id, "extracted_from"),
                    (a_id, c_id, "generated_from"),
                },
            )
            self.assertFalse(res.truncated)
            self.assertEqual(res.frontier, [])

    def test_cycle_terminates_without_infinite_loop(self) -> None:
        """Unexpected cycle A->B and B->A expands each node once and terminates."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            a_id = self._create_resource(conn, project_id, actor_id, "cycle_a")
            b_id = self._create_resource(
                conn, project_id, actor_id, "cycle_b", lineage=[(a_id, "derived_from")]
            )
            op_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO operations (id, project_id, kind, actor_id, status) "
                    "VALUES (%s, %s, %s, %s, 'succeeded')",
                    (op_id, project_id, "test.cycle", actor_id),
                )
                conn.execute(
                    "INSERT INTO lineage_edges (project_id, upstream_id, downstream_id, relation, operation_id) "
                    "VALUES (%s, %s, %s, 'derived_from', %s)",
                    (project_id, b_id, a_id, op_id),
                )

            up_res = walk(conn, project_id, a_id, direction="upstream", max_depth=5)
            self.assertEqual(set(up_res.nodes.keys()), {a_id, b_id})
            self.assertEqual(up_res.nodes[a_id].depth, 0)
            self.assertEqual(up_res.nodes[b_id].depth, 1)
            self.assertEqual(
                {(e.upstream_id, e.downstream_id) for e in up_res.edges},
                {(b_id, a_id), (a_id, b_id)},
            )
            self.assertFalse(up_res.truncated)
            self.assertEqual(up_res.frontier, [])

            down_res = walk(conn, project_id, a_id, direction="downstream", max_depth=5)
            self.assertEqual(set(down_res.nodes.keys()), {a_id, b_id})
            self.assertEqual(down_res.nodes[a_id].depth, 0)
            self.assertEqual(down_res.nodes[b_id].depth, 1)
            self.assertEqual(
                {(e.upstream_id, e.downstream_id) for e in down_res.edges},
                {(a_id, b_id), (b_id, a_id)},
            )
            self.assertFalse(down_res.truncated)
            self.assertEqual(down_res.frontier, [])

    def test_cross_project_and_invalid_parameters(self) -> None:
        """Cross-project start_id raises LookupError; out-of-range bounds raise ValueError."""
        with self.connect() as conn:
            project_1, actor_1 = self.make_project(conn)
            project_2, _actor_2 = self.make_project(conn)
            res_1 = self._create_resource(conn, project_1, actor_1, "isolated_res")

            with self.assertRaises(LookupError):
                walk(conn, project_2, res_1)

            with self.assertRaises(LookupError):
                walk(conn, project_1, uuid.uuid4())

            for bad_depth in (0, 11, -1, True):
                with self.subTest(max_depth=bad_depth):
                    with self.assertRaises(ValueError):
                        walk(conn, project_1, res_1, max_depth=bad_depth)

            for bad_nodes in (0, 1001, -5, False):
                with self.subTest(max_nodes=bad_nodes):
                    with self.assertRaises(ValueError):
                        walk(conn, project_1, res_1, max_nodes=bad_nodes)

            for bad_edges in (0, 10001, -1, True):
                with self.subTest(max_edges=bad_edges):
                    with self.assertRaises(ValueError):
                        walk(conn, project_1, res_1, max_edges=bad_edges)

            for bad_frontier in (0, 1001, -1, False):
                with self.subTest(max_frontier=bad_frontier):
                    with self.assertRaises(ValueError):
                        walk(conn, project_1, res_1, max_frontier=bad_frontier)

            with self.assertRaises(ValueError):
                walk(conn, project_1, res_1, direction="invalid")  # type: ignore[arg-type]

    def test_downstream_symmetry(self) -> None:
        """Downstream traversal mirrors upstream traversal on chains and diamonds."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            a_id = self._create_resource(conn, project_id, actor_id, "down_a")
            b_id = self._create_resource(
                conn, project_id, actor_id, "down_b", lineage=[(a_id, "derived_from")]
            )
            c_id = self._create_resource(
                conn, project_id, actor_id, "down_c", lineage=[(b_id, "derived_from")]
            )
            d_id = self._create_resource(
                conn, project_id, actor_id, "down_d", lineage=[(c_id, "derived_from")]
            )

            res_depth2 = walk(conn, project_id, a_id, direction="downstream", max_depth=2)
            self.assertEqual(set(res_depth2.nodes.keys()), {a_id, b_id, c_id})
            self.assertNotIn(d_id, res_depth2.nodes)
            self.assertEqual(res_depth2.nodes[a_id].depth, 0)
            self.assertEqual(res_depth2.nodes[b_id].depth, 1)
            self.assertEqual(res_depth2.nodes[c_id].depth, 2)
            self.assertEqual(
                [(e.upstream_id, e.downstream_id) for e in res_depth2.edges],
                [(a_id, b_id), (b_id, c_id)],
            )
            self.assertTrue(res_depth2.truncated)
            self.assertEqual(res_depth2.frontier, [d_id])

            res_depth3 = walk(conn, project_id, a_id, direction="downstream", max_depth=3)
            self.assertEqual(set(res_depth3.nodes.keys()), {a_id, b_id, c_id, d_id})
            self.assertEqual(res_depth3.nodes[d_id].depth, 3)
            self.assertEqual(len(res_depth3.edges), 3)
            self.assertFalse(res_depth3.truncated)
            self.assertEqual(res_depth3.frontier, [])

    def test_max_nodes_truncation(self) -> None:
        """Walk stops adding nodes at max_nodes, sets truncated=True, and reports frontier."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            a_id = self._create_resource(conn, project_id, actor_id, "limit_a")
            b1_id = self._create_resource(
                conn, project_id, actor_id, "limit_b1", lineage=[(a_id, "derived_from")]
            )
            b2_id = self._create_resource(conn, project_id, actor_id, "limit_b2")
            b3_id = self._create_resource(conn, project_id, actor_id, "limit_b3")
            d_id = self._create_resource(
                conn,
                project_id,
                actor_id,
                "limit_d",
                lineage=[
                    (b1_id, "derived_from"),
                    (b2_id, "derived_from"),
                    (b3_id, "derived_from"),
                ],
            )

            res_one = walk(conn, project_id, d_id, direction="upstream", max_depth=3, max_nodes=1)
            self.assertEqual(set(res_one.nodes.keys()), {d_id})
            self.assertEqual(res_one.edges, [])
            self.assertTrue(res_one.truncated)
            self.assertEqual(res_one.frontier, [b1_id, b2_id, b3_id])

            res_two = walk(conn, project_id, d_id, direction="upstream", max_depth=3, max_nodes=2)
            self.assertEqual(set(res_two.nodes.keys()), {d_id, b1_id})
            self.assertEqual(
                [(e.upstream_id, e.downstream_id) for e in res_two.edges],
                [(b1_id, d_id)],
            )
            self.assertTrue(res_two.truncated)
            self.assertEqual(res_two.frontier, [b2_id, b3_id, a_id])

    def test_max_frontier_truncation(self) -> None:
        """Walk truncates frontier collection at max_frontier."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            b1 = self._create_resource(conn, project_id, actor_id, "mf_b1")
            b2 = self._create_resource(conn, project_id, actor_id, "mf_b2")
            b3 = self._create_resource(conn, project_id, actor_id, "mf_b3")
            d = self._create_resource(
                conn, project_id, actor_id, "mf_d",
                lineage=[(b1, "derived_from"), (b2, "derived_from"), (b3, "derived_from")],
            )

            res = walk(conn, project_id, d, direction="upstream", max_nodes=1, max_frontier=2)
            self.assertEqual(len(res.frontier), 2)
            self.assertTrue(res.truncated)

    def test_multi_operation_edges_truncation(self) -> None:
        """Walk truncates edges collection at max_edges when multiple operations connect nodes."""
        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            p = self._create_resource(conn, project_id, actor_id, "mo_parent")
            c = self._create_resource(conn, project_id, actor_id, "mo_child", lineage=[(p, "derived_from")])
            with conn.transaction():
                for i in range(20):
                    op_id = uuid.uuid4()
                    conn.execute(
                        "INSERT INTO operations (id, project_id, kind, actor_id, status) "
                        "VALUES (%s, %s, %s, %s, 'succeeded')",
                        (op_id, project_id, f"test.mo_{i}", actor_id),
                    )
                    conn.execute(
                        "INSERT INTO lineage_edges (project_id, upstream_id, downstream_id, relation, operation_id) "
                        "VALUES (%s, %s, %s, 'derived_from', %s)",
                        (project_id, p, c, op_id),
                    )

            res = walk(conn, project_id, c, direction="upstream", max_edges=5)
            self.assertEqual(len(res.edges), 5)
            self.assertTrue(res.truncated)


if __name__ == "__main__":
    unittest.main()
