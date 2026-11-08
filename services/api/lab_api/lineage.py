"""GET /api/v1/resources/{resource_id}/lineage (SPEC 8, 10.2, 16.4).

Upstream or downstream lineage of one resource version, with a depth bound,
a node cap and cursor pagination. The traversal itself is
``lab_db.lineage.walk``; this module adds authorization, strict parameter
parsing, and a deterministic, tamper-evident paging order.

Authorization uses the ``read_gold_trace_and_lineage`` action (owner,
maintainer, reviewer). Lineage can expose gold-derived resources, so
participants and observers are refused. A resource that does not exist and a
resource in a project the caller is not a member of give the same 404.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

# documents puts db/ on sys.path, so it must be imported before lab_db.
from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db  # isort: skip

from lab_db import lineage as lineage_db  # noqa: E402
from lab_db.repo import NotFound, PermissionDenied, authorize  # noqa: E402

ACTION = "read_gold_trace_and_lineage"
DEFAULT_DEPTH = 3
DEFAULT_MAX_NODES = lineage_db.MAX_NODES
DEFAULT_LIMIT = 100
MIN_LIMIT, MAX_LIMIT = 1, 200
CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 256

router = APIRouter()


@dataclass(frozen=True)
class LineageQuery:
    resource_id: uuid.UUID
    direction: str
    depth: int
    max_nodes: int
    limit: int
    cursor: Optional[str]

    def binding(self) -> str:
        """Identifies the traversal a cursor belongs to (not the page size)."""
        text = f"{self.resource_id}|{self.direction}|{self.depth}|{self.max_nodes}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _int_param(params: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = params.get(name)
    if raw is None:
        return default
    # ASCII digits only: no sign, space, underscore or non-ASCII digits.
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 6:
        raise ApiError(400, "bad_request", f"{name} must be an integer between {low} and {high}")
    value = int(raw)
    if not low <= value <= high:
        raise ApiError(400, "bad_request", f"{name} must be an integer between {low} and {high}")
    return value


def parse_query(resource_id: uuid.UUID, items: "list[tuple[str, str]] | dict[str, str]") -> LineageQuery:
    pairs = list(items.items()) if isinstance(items, dict) else list(items)
    names = [k for k, _ in pairs]
    repeated = sorted({k for k in names if names.count(k) > 1})
    if repeated:
        raise ApiError(400, "bad_request", f"query parameter repeated: {repeated[0]}")
    params = dict(pairs)
    known = {"direction", "depth", "max_nodes", "limit", "cursor"}
    unknown = sorted(set(params) - known)
    if unknown:
        raise ApiError(400, "bad_request", f"unknown query parameter: {unknown[0]}")
    direction = params.get("direction", "upstream")
    if direction not in ("upstream", "downstream"):
        raise ApiError(400, "bad_request", "direction must be 'upstream' or 'downstream'")
    cursor = params.get("cursor")
    if cursor is not None and not (1 <= len(cursor) <= MAX_CURSOR_CHARS):
        raise ApiError(400, "bad_request", "invalid cursor")
    return LineageQuery(
        resource_id=resource_id,
        direction=direction,
        depth=_int_param(params, "depth", DEFAULT_DEPTH, lineage_db.MIN_DEPTH, lineage_db.MAX_DEPTH),
        max_nodes=_int_param(params, "max_nodes", DEFAULT_MAX_NODES, lineage_db.MIN_NODES, lineage_db.MAX_NODES),
        limit=_int_param(params, "limit", DEFAULT_LIMIT, MIN_LIMIT, MAX_LIMIT),
        cursor=cursor,
    )


def encode_cursor(query: LineageQuery, depth: int, node_id: uuid.UUID) -> str:
    payload = {"v": CURSOR_VERSION, "q": query.binding(), "d": depth, "id": str(node_id)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(query: LineageQuery, cursor: str) -> tuple[int, str]:
    """Return the (depth, node id) the page continues after, or 400."""
    bad = ApiError(400, "bad_request", "invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise bad from None
    if not isinstance(payload, dict) or set(payload) != {"v", "q", "d", "id"}:
        raise bad
    depth, node_id = payload["d"], payload["id"]
    if (
        payload["v"] != CURSOR_VERSION
        or payload["q"] != query.binding()
        or isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= lineage_db.MAX_DEPTH
        or not isinstance(node_id, str)
    ):
        raise bad
    try:
        normalized = str(uuid.UUID(node_id))
    except ValueError:
        raise bad from None
    if normalized != node_id:
        raise bad
    return depth, node_id


def build_page(query: LineageQuery, result: lineage_db.LineageResult) -> dict[str, Any]:
    """One page of a walk, in a deterministic order.

    Nodes are ordered by (depth, id). An edge is listed with the node at its
    farther end (greater depth, then greater id), so every edge appears on
    exactly one page and a page's edges always point at nodes it contains or
    at earlier pages.
    """
    depth = {nid: info.depth for nid, info in result.nodes.items()}
    key = lambda nid: (depth[nid], str(nid))  # noqa: E731
    ordered = sorted(result.nodes, key=key)

    start = 0
    if query.cursor is not None:
        after = decode_cursor(query, query.cursor)
        start = next((i for i, nid in enumerate(ordered) if key(nid) > after), len(ordered))
    page = ordered[start : start + query.limit]
    page_ids = set(page)
    has_more = start + query.limit < len(ordered)

    seen: set[tuple[str, str, str, str]] = set()
    edges = []
    for e in result.edges:
        ident = (str(e.upstream_id), str(e.downstream_id), e.relation, str(e.operation_id))
        if ident in seen:
            continue
        seen.add(ident)
        owner = max((e.upstream_id, e.downstream_id), key=key)
        if owner in page_ids:
            edges.append(
                {"upstream_id": ident[0], "downstream_id": ident[1], "relation": e.relation, "operation_id": ident[3]}
            )
    edges.sort(key=lambda x: (x["upstream_id"], x["downstream_id"], x["relation"], x["operation_id"]))

    nodes = []
    for nid in page:
        info = result.nodes[nid]
        nodes.append({
            "id": str(nid), "resource_type": info.resource_type, "logical_id": info.logical_id,
            "revision": info.revision, "content_hash": info.content_hash, "depth": info.depth,
        })
    return {
        "resource_id": str(query.resource_id),
        "direction": query.direction,
        "depth_limit": query.depth,
        "max_nodes": query.max_nodes,
        "nodes": nodes,
        "edges": edges,
        "next_cursor": encode_cursor(query, depth[page[-1]], page[-1]) if has_more and page else None,
        "truncated": result.truncated,
        "frontier_count": len(result.frontier),
    }


def _lineage_core(conn: psycopg.Connection, actor_id: uuid.UUID, query: LineageQuery) -> dict[str, Any]:
    """Blocking: authorize, walk, page."""
    row = conn.execute("SELECT project_id FROM resource_versions WHERE id = %s", (query.resource_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "resource not found")
    project_id = row[0]
    try:
        authorize(conn, project_id, actor_id, ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "resource not found")
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc))
    try:
        result = lineage_db.walk(
            conn, project_id, query.resource_id, query.direction, query.depth, query.max_nodes
        )
    except LookupError:
        raise ApiError(404, "not_found", "resource not found")
    # The cursor is checked after authorization so an unauthorized caller
    # learns nothing, and before any data is returned.
    page = build_page(query, result)
    page["project_id"] = str(project_id)
    return page


async def get_lineage_handler(
    resource_id: uuid.UUID,
    request: Request,
    conn: psycopg.Connection = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_authenticated_actor),
) -> JSONResponse:
    """Lineage of one resource version (SPEC 8: direction, depth, pagination, node cap)."""
    query = parse_query(resource_id, request.query_params.multi_items())
    content = await _run_blocking(request, "meta", _lineage_core, conn, actor_id, query)
    return JSONResponse(status_code=200, content=content)


router.add_api_route(
    "/api/v1/resources/{resource_id}/lineage", get_lineage_handler, methods=["GET"], name="get_resource_lineage_v1"
)
router.add_api_route(
    "/resources/{resource_id}/lineage", get_lineage_handler, methods=["GET"], name="get_resource_lineage_compat"
)
