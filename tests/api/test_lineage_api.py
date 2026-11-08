"""Unit tests for the lineage endpoint's parameter parsing, cursor and paging."""
from __future__ import annotations

import base64
import json
import sys
import unittest
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import lineage as api
from lab_api.documents import ApiError
from lab_db.lineage import EdgeInfo, LineageResult, NodeInfo

RID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def q(**params):
    return api.parse_query(RID, params)


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def node(depth: int) -> NodeInfo:
    return NodeInfo("document", "doc", 1, "ab" * 32, depth)


def edge(up: int, down: int, relation: str = "derived_from") -> EdgeInfo:
    return EdgeInfo(uid(up), uid(down), relation, uuid.UUID(int=1000 + up * 10 + down))


def diamond(order=None) -> LineageResult:
    """start=1 (depth 0); 2,3 (depth 1); 4 (depth 2) reachable from 2 and 3;
    5 (depth 2). Upstream walk: 1 <- 2,3 ; 2 <- 4 ; 3 <- 4 ; 3 <- 5."""
    nodes = {1: node(0), 2: node(1), 3: node(1), 4: node(2), 5: node(2)}
    edges = [edge(2, 1), edge(3, 1), edge(4, 2), edge(4, 3), edge(5, 3)]
    if order:
        nodes = {k: nodes[k] for k in order}
        edges = list(reversed(edges))
    return LineageResult(nodes={uid(k): v for k, v in nodes.items()}, edges=edges, truncated=False, frontier=[])


class ParameterTests(unittest.TestCase):
    def test_defaults(self):
        got = q()
        self.assertEqual((got.direction, got.depth, got.max_nodes, got.limit, got.cursor), ("upstream", 3, 1000, 100, None))

    def test_valid_values_and_bounds(self):
        got = q(direction="downstream", depth="10", max_nodes="1", limit="200")
        self.assertEqual((got.direction, got.depth, got.max_nodes, got.limit), ("downstream", 10, 1, 200))
        self.assertEqual(q(depth="1").depth, 1)

    def test_invalid_values_are_400_bad_request(self):
        bad = [
            {"direction": "sideways"}, {"direction": ""}, {"direction": "UPSTREAM"},
            {"depth": "0"}, {"depth": "11"}, {"depth": "abc"}, {"depth": "-1"}, {"depth": "+2"}, {"depth": "1_0"},
            {"depth": " 2"}, {"depth": ""}, {"depth": "2.0"}, {"depth": "٣"},  # Arabic-indic digit three
            {"depth": "0000001"},  # longer than the digit cap
            {"max_nodes": "0"}, {"max_nodes": "1001"}, {"limit": "0"}, {"limit": "201"}, {"limit": "x"},
            {"cursor": ""}, {"cursor": "a" * 257},
            {"surprise": "1"},
        ]
        for params in bad:
            with self.subTest(params=params):
                with self.assertRaises(ApiError) as ctx:
                    q(**params)
                self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

    def test_repeated_parameters_are_rejected_not_collapsed(self):
        with self.assertRaises(ApiError) as ctx:
            api.parse_query(RID, [("depth", "1"), ("depth", "2")])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("repeated", ctx.exception.message)


class CursorTests(unittest.TestCase):
    def test_round_trip(self):
        query = q(depth="4")
        cursor = api.encode_cursor(query, 2, uid(7))
        self.assertEqual(api.decode_cursor(query, cursor), (2, str(uid(7))))

    def forge(self, **overrides):
        payload = {"v": 1, "q": q().binding(), "d": 1, "id": str(uid(3))}
        payload.update(overrides)
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def test_a_forged_but_well_formed_cursor_is_accepted_only_with_the_right_binding(self):
        self.assertEqual(api.decode_cursor(q(), self.forge()), (1, str(uid(3))))

    def test_tampered_or_foreign_cursors_are_400(self):
        base = q()
        cases = {
            "not base64": "!!!notbase64!!!",
            "not json": base64.urlsafe_b64encode(b"nope").decode().rstrip("="),
            "json list": base64.urlsafe_b64encode(b"[1]").decode().rstrip("="),
            "other traversal": self.forge(q="0" * 16),
            "wrong version": self.forge(v=2),
            "bool depth": self.forge(d=True),
            "depth out of range": self.forge(d=11),
            "negative depth": self.forge(d=-1),
            "string depth": self.forge(d="1"),
            "id not a string": self.forge(id=5),
            "id not a uuid": self.forge(id="zzz"),
            "id not canonical": self.forge(id=str(uid(0xabc)).upper()),
            "extra key": self.forge(extra=1),
        }
        for label, cursor in cases.items():
            with self.subTest(label):
                with self.assertRaises(ApiError) as ctx:
                    api.decode_cursor(base, cursor)
                self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

    def test_a_cursor_from_another_depth_or_direction_does_not_transfer(self):
        cursor = api.encode_cursor(q(depth="3"), 1, uid(2))
        for other in (q(depth="4"), q(direction="downstream"), q(max_nodes="10")):
            with self.assertRaises(ApiError):
                api.decode_cursor(other, cursor)
        # The page size is not part of the traversal, so it may change between pages.
        api.decode_cursor(q(depth="3", limit="5"), cursor)


class PagingTests(unittest.TestCase):
    def pages(self, result, limit):
        out, cursor = [], None
        for _ in range(20):
            page = api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, limit, cursor), result)
            out.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                return out
        self.fail("paging did not terminate")

    def test_every_node_and_edge_appears_exactly_once_in_a_stable_order(self):
        result = diamond()
        for limit in (1, 2, 3, 5, 100):
            with self.subTest(limit=limit):
                pages = self.pages(result, limit)
                ids = [n["id"] for p in pages for n in p["nodes"]]
                self.assertEqual(ids, sorted(ids, key=lambda i: (result.nodes[uuid.UUID(i)].depth, i)))
                self.assertEqual(sorted(ids), sorted(str(k) for k in result.nodes))
                self.assertEqual(len(ids), len(set(ids)))
                edges = [(e["upstream_id"], e["downstream_id"]) for p in pages for e in p["edges"]]
                self.assertEqual(sorted(edges), sorted((str(e.upstream_id), str(e.downstream_id)) for e in result.edges))
                self.assertEqual(len(edges), len(set(edges)))
                self.assertTrue(all(len(p["nodes"]) <= limit for p in pages))
                self.assertEqual([p["next_cursor"] is None for p in pages], [False] * (len(pages) - 1) + [True])

    def test_edges_are_listed_with_the_node_at_their_far_end(self):
        pages = self.pages(diamond(), 1)
        by_node = {p["nodes"][0]["id"]: p for p in pages}
        depth = {str(k): v.depth for k, v in diamond().nodes.items()}
        for page in pages:
            here = page["nodes"][0]["id"]
            for e in page["edges"]:
                far = max((e["upstream_id"], e["downstream_id"]), key=lambda i: (depth[i], i))
                self.assertEqual(far, here)
        self.assertEqual(sum(len(p["edges"]) for p in by_node.values()), 5)

    def test_result_does_not_depend_on_the_walk_order(self):
        a = self.pages(diamond(), 2)
        b = self.pages(diamond(order=[5, 4, 3, 2, 1]), 2)
        self.assertEqual(a, b)

    def test_single_page_has_no_cursor_and_metadata_is_passed_through(self):
        result = LineageResult(nodes=diamond().nodes, edges=diamond().edges, truncated=True, frontier=[uid(9), uid(10)])
        page = api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 100, None), result)
        self.assertEqual((page["next_cursor"], page["truncated"], page["frontier_count"]), (None, True, 2))
        self.assertEqual((page["direction"], page["depth_limit"], page["max_nodes"]), ("upstream", 3, 1000))

    def test_a_cursor_past_the_end_gives_an_empty_final_page(self):
        query = api.LineageQuery(RID, "upstream", 3, 1000, 5, None)
        cursor = api.encode_cursor(query, 2, uid(2 ** 100))
        page = api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 5, cursor), diamond())
        self.assertEqual((page["nodes"], page["edges"], page["next_cursor"]), ([], [], None))

    def test_a_foreign_cursor_is_refused_by_build_page(self):
        other = api.LineageQuery(RID, "downstream", 3, 1000, 5, None)
        cursor = api.encode_cursor(other, 1, uid(2))
        with self.assertRaises(ApiError):
            api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 5, cursor), diamond())


class PagingEdgeCaseTests(unittest.TestCase):
    def test_a_duplicated_edge_is_listed_once(self):
        e = edge(2, 1)
        result = LineageResult(nodes={uid(1): node(0), uid(2): node(1)}, edges=[e, e, e], truncated=False, frontier=[])
        page = api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 10, None), result)
        self.assertEqual(len(page["edges"]), 1)

    def test_order_is_depth_first_even_when_a_deeper_node_has_the_smaller_id(self):
        result = LineageResult(
            nodes={uid(9): node(0), uid(1): node(1), uid(5): node(1)},
            edges=[edge(1, 9), edge(5, 9)], truncated=False, frontier=[],
        )
        pages = []
        cursor = None
        while True:
            page = api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 1, cursor), result)
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual([p["nodes"][0]["id"] for p in pages], [str(uid(9)), str(uid(1)), str(uid(5))])
        self.assertEqual([p["nodes"][0]["depth"] for p in pages], [0, 1, 1])


if __name__ == "__main__":
    unittest.main()
