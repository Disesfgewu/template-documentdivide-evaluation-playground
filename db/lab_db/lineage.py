"""Bounded lineage graph traversal over resource_versions and lineage_edges."""

from dataclasses import dataclass
from typing import Literal
import uuid

import psycopg

Direction = Literal["upstream", "downstream"]

MIN_DEPTH = 1
MAX_DEPTH = 10
MIN_NODES = 1
MAX_NODES = 1000
MIN_EDGES = 1
MAX_EDGES = 10000
DEFAULT_MAX_EDGES = 2000
MIN_FRONTIER = 1
MAX_FRONTIER = 1000
DEFAULT_MAX_FRONTIER = 1000


@dataclass(frozen=True)
class NodeInfo:
    """Metadata and shortest-path distance for a visited resource version."""

    resource_type: str
    logical_id: str
    revision: int
    content_hash: str
    depth: int


@dataclass(frozen=True)
class EdgeInfo:
    """A traversed lineage edge between two visited resource versions."""

    upstream_id: uuid.UUID
    downstream_id: uuid.UUID
    relation: str
    operation_id: uuid.UUID


@dataclass(frozen=True)
class LineageResult:
    """Result of a bounded lineage walk."""

    nodes: dict[uuid.UUID, NodeInfo]
    edges: list[EdgeInfo]
    truncated: bool
    frontier: list[uuid.UUID]


def _validate_args(
    direction: str,
    max_depth: int,
    max_nodes: int,
    max_edges: int = DEFAULT_MAX_EDGES,
    max_frontier: int = DEFAULT_MAX_FRONTIER,
) -> None:
    """Validate traversal direction and bounds."""
    if direction not in ("upstream", "downstream"):
        raise ValueError(f"direction must be 'upstream' or 'downstream', got {direction!r}")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not (MIN_DEPTH <= max_depth <= MAX_DEPTH):
        raise ValueError(f"max_depth must be an int between {MIN_DEPTH} and {MAX_DEPTH}, got {max_depth!r}")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not (MIN_NODES <= max_nodes <= MAX_NODES):
        raise ValueError(f"max_nodes must be an int between {MIN_NODES} and {MAX_NODES}, got {max_nodes!r}")
    if isinstance(max_edges, bool) or not isinstance(max_edges, int) or not (MIN_EDGES <= max_edges <= MAX_EDGES):
        raise ValueError(f"max_edges must be an int between {MIN_EDGES} and {MAX_EDGES}, got {max_edges!r}")
    if isinstance(max_frontier, bool) or not isinstance(max_frontier, int) or not (MIN_FRONTIER <= max_frontier <= MAX_FRONTIER):
        raise ValueError(f"max_frontier must be an int between {MIN_FRONTIER} and {MAX_FRONTIER}, got {max_frontier!r}")


def _fetch_edges(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    direction: Direction,
    limit: int | None = None,
) -> list[tuple[int, uuid.UUID, uuid.UUID, str, uuid.UUID]]:
    """Fetch lineage edges incident to source_ids in the requested direction."""
    if not source_ids:
        return []
    if limit is not None and limit <= 0:
        return []
    fetch_limit = limit if limit is not None else DEFAULT_MAX_EDGES + 1
    if direction == "upstream":
        sql = (
            "SELECT id, upstream_id, downstream_id, relation::text, operation_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND downstream_id = ANY(%s) "
            "ORDER BY id ASC "
            "LIMIT %s"
        )
    else:
        sql = (
            "SELECT id, upstream_id, downstream_id, relation::text, operation_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND upstream_id = ANY(%s) "
            "ORDER BY id ASC "
            "LIMIT %s"
        )
    rows = conn.execute(sql, (project_id, source_ids, fetch_limit)).fetchall()
    return [
        (int(row[0]), row[1], row[2], str(row[3]), row[4])
        for row in rows
    ]


def _fetch_boundary_frontier(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    direction: Direction,
    visited_ids: set[uuid.UUID],
    limit: int,
) -> tuple[list[uuid.UUID], bool]:
    """Fetch unvisited neighbor node IDs directly adjacent to source_ids.

    Uses GROUP BY in SQL to deduplicate multi-operation edges between endpoints
    and LIMIT to strictly bound database read volume. Returns (frontier_ids, has_more).
    """
    if not source_ids or limit <= 0:
        return [], False
    visited_list = list(visited_ids)
    if direction == "upstream":
        sql = (
            "SELECT MIN(id) AS first_id, upstream_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND downstream_id = ANY(%s) "
        )
        params: list[object] = [project_id, source_ids]
        if visited_list:
            sql += "AND NOT (upstream_id = ANY(%s)) "
            params.append(visited_list)
        sql += "GROUP BY upstream_id ORDER BY MIN(id) ASC LIMIT %s"
        params.append(limit + 1)
    else:
        sql = (
            "SELECT MIN(id) AS first_id, downstream_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND upstream_id = ANY(%s) "
        )
        params = [project_id, source_ids]
        if visited_list:
            sql += "AND NOT (downstream_id = ANY(%s)) "
            params.append(visited_list)
        sql += "GROUP BY downstream_id ORDER BY MIN(id) ASC LIMIT %s"
        params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()
    frontier = [row[1] for row in rows[:limit]]
    has_more = len(rows) > limit
    return frontier, has_more


def walk(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    start_id: uuid.UUID,
    direction: Direction = "upstream",
    max_depth: int = 3,
    max_nodes: int = 1000,
    max_edges: int = DEFAULT_MAX_EDGES,
    max_frontier: int = DEFAULT_MAX_FRONTIER,
) -> LineageResult:
    """Walk lineage edges from ``start_id`` within ``project_id``.

    Each reachable resource version is expanded at most once in breadth-first
    order so DAG diamonds record shortest distance and cycles terminate cleanly.
    Traversal, database reads, and frontier collection are strictly bounded.
    """
    _validate_args(direction, max_depth, max_nodes, max_edges, max_frontier)

    start_row = conn.execute(
        "SELECT id, resource_type, logical_id, revision, content_hash "
        "FROM resource_versions "
        "WHERE project_id = %s AND id = %s",
        (project_id, start_id),
    ).fetchone()
    if start_row is None:
        raise LookupError(f"resource version {start_id} not found")

    depths: dict[uuid.UUID, int] = {start_id: 0}
    edges: list[EdgeInfo] = []
    frontier: list[uuid.UUID] = []
    frontier_set: set[uuid.UUID] = set()
    current_level: list[uuid.UUID] = [start_id]
    truncated = False

    for depth in range(max_depth):
        if not current_level or len(depths) >= max_nodes or len(edges) >= max_edges:
            break

        edge_budget = max_edges - len(edges)
        edge_rows = _fetch_edges(conn, project_id, current_level, direction, limit=edge_budget + 1)
        next_level: list[uuid.UUID] = []

        if len(edge_rows) > edge_budget:
            truncated = True
            edge_rows = edge_rows[:edge_budget]

        for _edge_id, upstream_id, downstream_id, relation, operation_id in edge_rows:
            neighbor_id = upstream_id if direction == "upstream" else downstream_id
            if neighbor_id in depths:
                edges.append(
                    EdgeInfo(
                        upstream_id=upstream_id,
                        downstream_id=downstream_id,
                        relation=relation,
                        operation_id=operation_id,
                    )
                )
            elif len(depths) < max_nodes:
                depths[neighbor_id] = depth + 1
                next_level.append(neighbor_id)
                edges.append(
                    EdgeInfo(
                        upstream_id=upstream_id,
                        downstream_id=downstream_id,
                        relation=relation,
                        operation_id=operation_id,
                    )
                )
            else:
                if len(frontier) < max_frontier:
                    if neighbor_id not in frontier_set:
                        frontier_set.add(neighbor_id)
                        frontier.append(neighbor_id)
                else:
                    truncated = True

        current_level = next_level
        if len(edges) >= max_edges:
            truncated = True
            break

    if current_level and len(frontier) < max_frontier:
        frontier_budget = max_frontier - len(frontier)
        boundary_frontier, has_more = _fetch_boundary_frontier(
            conn, project_id, current_level, direction, set(depths.keys()) | frontier_set, frontier_budget
        )
        for nid in boundary_frontier:
            if nid not in frontier_set:
                frontier_set.add(nid)
                frontier.append(nid)
        if has_more:
            truncated = True
    elif current_level and len(frontier) >= max_frontier:
        truncated = True

    node_ids = list(depths.keys())
    version_rows = conn.execute(
        "SELECT id, resource_type, logical_id, revision, content_hash "
        "FROM resource_versions "
        "WHERE project_id = %s AND id = ANY(%s)",
        (project_id, node_ids),
    ).fetchall()
    row_by_id = {
        row[0]: (str(row[1]), str(row[2]), int(row[3]), str(row[4]))
        for row in version_rows
    }

    nodes: dict[uuid.UUID, NodeInfo] = {}
    for nid, dist in depths.items():
        rtype, lid, rev, chash = row_by_id[nid]
        nodes[nid] = NodeInfo(
            resource_type=rtype,
            logical_id=lid,
            revision=rev,
            content_hash=chash,
            depth=dist,
        )

    return LineageResult(
        nodes=nodes,
        edges=edges,
        truncated=truncated or bool(frontier),
        frontier=frontier,
    )
