"""Truncation semantics of ``lineage.walk`` against an in-memory model of its SQL.

The fake connection answers the four statements ``walk`` issues exactly as the
SQL reads (project filter, direction column, ``ORDER BY id`` / ``MIN(id)``,
limits). It checks the algorithm, not PostgreSQL: the same graphs run on real
PostgreSQL in ``tests/db/test_lineage.py``.
"""
from __future__ import annotations

import unittest
import uuid

from lab_db.lineage import walk

P = uuid.uuid4()


def rid() -> uuid.UUID:
    return uuid.uuid4()


class FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    """edges: (id, upstream, downstream, relation, operation)."""

    def __init__(self, names: dict[uuid.UUID, str], edges: list[tuple]):
        self.names = names
        self.edges = edges
        self.statements: list[str] = []

    def execute(self, sql, params):
        self.statements.append(sql)
        project, *rest = params
        assert project == P
        if "FROM resource_versions" in sql:
            ids = [rest[0]] if "AND id = %s" in sql else rest[0]
            return FakeRows([(i, "document", self.names[i], 1, "h" * 64) for i in ids if i in self.names])
        upstream = "project_id = %s AND downstream_id = ANY" in sql  # walking upstream matches on the downstream column
        near, far = (2, 1) if upstream else (1, 2)  # tuple positions
        if sql.startswith("SELECT 1 FROM lineage_edges"):
            ids = rest[0]
            return FakeRows([(1,)] if any(e[near] in ids for e in self.edges) else [])
        if sql.startswith("SELECT MIN(id)"):
            ids, limit = rest[0], rest[-1]
            visited = set(rest[1]) if len(rest) == 3 else set()
            first: dict[uuid.UUID, int] = {}
            for e in sorted(self.edges):
                if e[near] in ids and e[far] not in visited:
                    first.setdefault(e[far], e[0])
            ordered = sorted(first.items(), key=lambda kv: kv[1])[:limit]
            return FakeRows([(i, n) for n, i in ordered])
        if sql.startswith("SELECT id, upstream_id"):
            ids, limit = rest
            rows = [(e[0], e[1], e[2], e[3], e[4]) for e in sorted(self.edges) if e[near] in ids]
            return FakeRows(rows[:limit])
        raise AssertionError(f"unexpected statement: {sql}")


def graph(*pairs, parallel: dict[int, int] | None = None):
    """pairs: (upstream_name, downstream_name); returns (conn, ids by name)."""
    ids: dict[str, uuid.UUID] = {}
    for a, b in pairs:
        ids.setdefault(a, rid())
        ids.setdefault(b, rid())
    edges, n = [], 0
    for i, (a, b) in enumerate(pairs):
        for _ in range((parallel or {}).get(i, 1)):
            n += 1
            edges.append((n, ids[a], ids[b], "derived_from", rid()))
    return FakeConn({v: k for k, v in ids.items()}, edges), ids


class ExactCapTests(unittest.TestCase):
    def test_exact_edge_cap_on_a_complete_graph_is_not_truncated(self):
        # PM finding: root -> child, nothing else. Cap 1 used exactly, nothing omitted.
        for cap in (1, 2, 5):
            with self.subTest(cap=cap):
                conn, ids = graph(("root", "child"))
                res = walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=cap)
                self.assertEqual(set(res.nodes), {ids["root"], ids["child"]})
                self.assertEqual(len(res.edges), 1)
                self.assertFalse(res.truncated)
                self.assertEqual(res.frontier, [])

    def test_one_more_edge_beyond_the_cap_is_truncated(self):
        conn, ids = graph(("root", "child"), ("child", "grand"))
        res = walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=1)
        self.assertEqual(len(res.edges), 1)
        self.assertTrue(res.truncated)
        self.assertEqual(res.frontier, [ids["grand"]])
        again = walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=2)
        self.assertFalse(again.truncated)
        self.assertEqual(len(again.edges), 2)

    def test_omitted_edge_back_to_a_visited_node_is_truncation(self):
        # The only omitted edge ends at an already visited node, so the frontier query cannot see it.
        conn, ids = graph(("root", "child"), ("child", "root"))
        capped = walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=1)
        self.assertEqual(len(capped.edges), 1)
        self.assertEqual(capped.frontier, [])
        self.assertTrue(capped.truncated)
        full = walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=2)
        self.assertEqual(len(full.edges), 2)
        self.assertFalse(full.truncated)

    def test_chain_exact_and_one_over(self):
        conn, ids = graph(("a", "b"), ("b", "c"), ("c", "d"))
        self.assertFalse(walk(conn, P, ids["a"], direction="downstream", max_depth=5, max_edges=3).truncated)
        self.assertTrue(walk(conn, P, ids["a"], direction="downstream", max_depth=5, max_edges=2).truncated)

    def test_parallel_operations_exact_and_one_over(self):
        conn5, ids5 = graph(("p", "c"), parallel={0: 5})
        res = walk(conn5, P, ids5["c"], direction="upstream", max_edges=5)
        self.assertEqual(len(res.edges), 5)
        self.assertFalse(res.truncated)
        conn6, ids6 = graph(("p", "c"), parallel={0: 6})
        res = walk(conn6, P, ids6["c"], direction="upstream", max_edges=5)
        self.assertEqual(len(res.edges), 5)
        self.assertTrue(res.truncated)

    def test_upstream_direction(self):
        conn, ids = graph(("a", "b"), ("b", "c"))
        self.assertFalse(walk(conn, P, ids["c"], direction="upstream", max_depth=3, max_edges=2).truncated)
        self.assertTrue(walk(conn, P, ids["c"], direction="upstream", max_depth=3, max_edges=1).truncated)


class DepthLimitTests(unittest.TestCase):
    def test_depth_limit_exact_and_one_over(self):
        conn, ids = graph(("a", "b"), ("b", "c"), ("c", "d"))
        exact = walk(conn, P, ids["a"], direction="downstream", max_depth=3)
        self.assertFalse(exact.truncated)
        self.assertEqual(len(exact.nodes), 4)
        short = walk(conn, P, ids["a"], direction="downstream", max_depth=2)
        self.assertTrue(short.truncated)
        self.assertEqual(short.frontier, [ids["d"]])

    def test_unexpanded_last_level_with_a_back_edge_is_truncation(self):
        conn, ids = graph(("a", "b"), ("b", "c"), ("c", "a"))
        cut = walk(conn, P, ids["a"], direction="downstream", max_depth=2)
        self.assertEqual(set(cut.nodes), {ids["a"], ids["b"], ids["c"]})
        self.assertEqual(cut.frontier, [])
        self.assertTrue(cut.truncated)  # c -> a was never fetched
        done = walk(conn, P, ids["a"], direction="downstream", max_depth=3)
        self.assertEqual(len(done.edges), 3)
        self.assertFalse(done.truncated)

    def test_cycle_and_diamond_stay_complete(self):
        conn, ids = graph(("a", "b"), ("b", "a"))
        for direction in ("upstream", "downstream"):
            res = walk(conn, P, ids["a"], direction=direction, max_depth=5)
            self.assertEqual((len(res.nodes), len(res.edges), res.truncated), (2, 2, False))
        conn, ids = graph(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
        res = walk(conn, P, ids["d"], direction="upstream", max_depth=3)
        self.assertEqual((len(res.nodes), len(res.edges), res.truncated, res.frontier), (4, 4, False, []))


class OtherBoundsStayTests(unittest.TestCase):
    def test_node_cap_still_truncates_and_reports_frontier(self):
        conn, ids = graph(("b1", "d"), ("b2", "d"), ("b3", "d"))
        res = walk(conn, P, ids["d"], direction="upstream", max_nodes=1, max_frontier=2)
        self.assertEqual(len(res.frontier), 2)
        self.assertTrue(res.truncated)

    def test_the_extra_probe_is_one_bounded_statement(self):
        conn, ids = graph(("root", "child"))
        walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=1)
        probes = [s for s in conn.statements if s.startswith("SELECT 1 FROM lineage_edges")]
        self.assertEqual(len(probes), 1)
        self.assertIn("LIMIT 1", probes[0])
        self.assertIn("project_id = %s", probes[0])

    def test_no_probe_when_truncation_is_already_known(self):
        conn, ids = graph(("root", "child"), ("child", "grand"))
        walk(conn, P, ids["root"], direction="downstream", max_depth=3, max_edges=1)
        self.assertEqual([s for s in conn.statements if s.startswith("SELECT 1 FROM lineage_edges")], [])


if __name__ == "__main__":
    unittest.main()
