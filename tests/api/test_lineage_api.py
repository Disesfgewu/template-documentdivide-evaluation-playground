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
        self.assertEqual(
            (got.direction, got.depth, got.max_nodes, got.limit, got.cursor, got.max_edges),
            ("upstream", 3, 1000, 100, None, 2000),
        )

    def test_valid_values_and_bounds(self):
        got = q(direction="downstream", depth="10", max_nodes="1", limit="200", max_edges="50")
        self.assertEqual((got.direction, got.depth, got.max_nodes, got.limit, got.max_edges), ("downstream", 10, 1, 200, 50))
        self.assertEqual(q(depth="1").depth, 1)

    def test_invalid_values_are_400_bad_request(self):
        bad = [
            {"direction": "sideways"}, {"direction": ""}, {"direction": "UPSTREAM"},
            {"depth": "0"}, {"depth": "11"}, {"depth": "abc"}, {"depth": "-1"}, {"depth": "+2"}, {"depth": "1_0"},
            {"depth": " 2"}, {"depth": ""}, {"depth": "2.0"}, {"depth": "٣"},  # Arabic-indic digit three
            {"depth": "0000001"},  # longer than the digit cap
            {"max_nodes": "0"}, {"max_nodes": "1001"}, {"limit": "0"}, {"limit": "201"}, {"limit": "x"},
            {"max_edges": "0"}, {"max_edges": "10001"},
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

    def test_round_trip_with_custom_secret(self):
        secret = b"custom-secret-key-12345678"
        query = q(depth="4")
        cursor = api.encode_cursor(query, 2, uid(7), secret=secret)
        self.assertEqual(api.decode_cursor(query, cursor, secret=secret), (2, str(uid(7))))

    def forge(self, **overrides):
        binding = q().binding()
        sig = api._cursor_signature(api.DEFAULT_CURSOR_SECRET, binding, 1, str(uid(3)))
        payload = {"v": 1, "q": binding, "d": 1, "id": str(uid(3)), "s": sig}
        payload.update(overrides)
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def test_a_signed_cursor_is_accepted_with_the_right_binding_and_signature(self):
        self.assertEqual(api.decode_cursor(q(), self.forge()), (1, str(uid(3))))

    def test_tampered_cursor_preserving_q_is_refused_with_400(self):
        """P2 regression: preserving q while modifying depth or node id must fail with 400."""
        valid = api.encode_cursor(q(), 1, uid(3))
        raw = base64.urlsafe_b64decode(valid + "=" * (-len(valid) % 4))
        payload = json.loads(raw)
        # Preserve q, alter depth and id to unvisited / non-existent target
        payload["d"] = 10
        payload["id"] = str(uid(999))
        tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        with self.assertRaises(ApiError) as ctx:
            api.decode_cursor(q(), tampered)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

    def test_cursor_signed_with_different_secret_is_refused(self):
        cursor = api.encode_cursor(q(), 1, uid(3), secret=b"secret-one")
        with self.assertRaises(ApiError) as ctx:
            api.decode_cursor(q(), cursor, secret=b"secret-two")
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

    def test_tampered_or_foreign_cursors_are_400(self):
        base = q()
        valid = api.encode_cursor(base, 1, uid(3))
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
            "missing signature": base64.urlsafe_b64encode(
                json.dumps({"v": 1, "q": base.binding(), "d": 1, "id": str(uid(3))}).encode()
            ).rstrip(b"=").decode(),
            "wrong signature": self.forge(s="0" * 32),
            "non-ascii signature": self.forge(s="é"),
            "short signature": self.forge(s="abc"),
            "uppercase signature": self.forge(s="A" * 32),
            "invalid hex signature": self.forge(s="g" * 32),
            "invalid alphabet suffix": valid + "!!!!",
            "non-canonical padding": valid + "=",
        }
        for label, cursor in cases.items():
            with self.subTest(label):
                with self.assertRaises(ApiError) as ctx:
                    api.decode_cursor(base, cursor)
                self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

    def test_a_cursor_from_another_depth_or_direction_does_not_transfer(self):
        cursor = api.encode_cursor(q(depth="3"), 1, uid(2))
        for other in (q(depth="4"), q(direction="downstream"), q(max_nodes="10"), q(max_edges="50")):
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

    def test_cursor_referencing_nonexistent_node_in_graph_is_refused(self):
        query = api.LineageQuery(RID, "upstream", 3, 1000, 5, None)
        cursor = api.encode_cursor(query, 2, uid(2 ** 100))
        with self.assertRaises(ApiError) as ctx:
            api.build_page(api.LineageQuery(RID, "upstream", 3, 1000, 5, cursor), diamond())
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "bad_request"))

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

    def test_multi_operation_edges_bounded_by_max_edges(self):
        e1 = edge(2, 1)
        e2 = EdgeInfo(uid(2), uid(1), "derived_from", uuid.UUID(int=9999))
        result = LineageResult(nodes={uid(1): node(0), uid(2): node(1)}, edges=[e1, e2], truncated=False, frontier=[])
        query = api.LineageQuery(RID, "upstream", 3, 1000, 10, None, max_edges=1)
        page = api.build_page(query, result)
        self.assertEqual(len(page["edges"]), 1)
        self.assertTrue(page["truncated"])

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


class CursorAdmissionTests(unittest.TestCase):
    def test_invalid_cursor_is_rejected_before_graph_walk(self):
        from unittest.mock import MagicMock, patch
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (uid(99),)
        cursors = ["invalid!", api.encode_cursor(q(), 1, uid(2), secret=b"wrong-secret"),
                   api.encode_cursor(q(direction="downstream"), 1, uid(2))]
        for cursor in cursors:
            with self.subTest(cursor=cursor), patch.object(api, "authorize"), \
                 patch.object(api.lineage_db, "walk", side_effect=RuntimeError("walk invoked")) as walk:
                with self.assertRaises(ApiError) as raised:
                    api._lineage_core(conn, uid(9), q(cursor=cursor))
                self.assertEqual(raised.exception.status_code, 400)
                walk.assert_not_called()

    def test_http_invalid_cursor_does_not_walk(self):
        from unittest.mock import MagicMock, patch
        from fastapi.testclient import TestClient
        from lab_api.config import Settings
        from lab_api.main import create_app
        from lab_api.documents import get_db, get_authenticated_actor
        settings = Settings(env="test", provider_mode="mock", database_url="postgresql://lab:test@localhost/lab",
                            artifact_root="/tmp/lineage-cursor-tests", auth_secret="cursor-test-secret-32-characters-long")
        app = create_app(settings, checks={})
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (uid(99),)
        app.dependency_overrides[get_db] = lambda: conn
        app.dependency_overrides[get_authenticated_actor] = lambda: uid(9)
        with patch.object(api, "authorize"), patch.object(api.lineage_db, "walk") as walk:
            response = TestClient(app).get(f"/api/v1/resources/{RID}/lineage?cursor=invalid!")
        self.assertEqual(response.status_code, 400)
        walk.assert_not_called()
