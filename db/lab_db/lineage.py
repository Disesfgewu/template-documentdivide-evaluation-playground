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


def _validate_args(direction: str, max_depth: int, max_nodes: int) -> None:
    """Validate traversal direction and bounds."""
    if direction not in ("upstream", "downstream"):
        raise ValueError(f"direction must be 'upstream' or 'downstream', got {direction!r}")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not (MIN_DEPTH <= max_depth <= MAX_DEPTH):
        raise ValueError(f"max_depth must be an int between {MIN_DEPTH} and {MAX_DEPTH}, got {max_depth!r}")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not (MIN_NODES <= max_nodes <= MAX_NODES):
        raise ValueError(f"max_nodes must be an int between {MIN_NODES} and {MAX_NODES}, got {max_nodes!r}")


def _fetch_edges(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    direction: Direction,
) -> list[tuple[int, uuid.UUID, uuid.UUID, str, uuid.UUID]]:
    """Fetch lineage edges incident to source_ids in the requested direction."""
    if not source_ids:
        return []
    if direction == "upstream":
        sql = (
            "SELECT id, upstream_id, downstream_id, relation::text, operation_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND downstream_id = ANY(%s) "
            "ORDER BY id ASC"
        )
    else:
        sql = (
            "SELECT id, upstream_id, downstream_id, relation::text, operation_id "
            "FROM lineage_edges "
            "WHERE project_id = %s AND upstream_id = ANY(%s) "
            "ORDER BY id ASC"
        )
    rows = conn.execute(sql, (project_id, source_ids)).fetchall()
    return [
        (int(row[0]), row[1], row[2], str(row[3]), row[4])
        for row in rows
    ]


def walk(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    start_id: uuid.UUID,
    direction: Direction = "upstream",
    max_depth: int = 3,
    max_nodes: int = 1000,
) -> LineageResult:
    """Walk lineage edges from ``start_id`` within ``project_id``.

    Each reachable resource version is expanded at most once in breadth-first
    order so DAG diamonds record shortest distance and cycles terminate cleanly.
    """
    _validate_args(direction, max_depth, max_nodes)

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

    for depth in range(max_depth):
        if not current_level or len(depths) >= max_nodes:
            break

        edge_rows = _fetch_edges(conn, project_id, current_level, direction)
        next_level: list[uuid.UUID] = []

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
                if neighbor_id not in frontier_set:
                    frontier_set.add(neighbor_id)
                    frontier.append(neighbor_id)

        current_level = next_level

    if current_level:
        boundary_rows = _fetch_edges(conn, project_id, current_level, direction)
        for _edge_id, upstream_id, downstream_id, _relation, _operation_id in boundary_rows:
            neighbor_id = upstream_id if direction == "upstream" else downstream_id
            if neighbor_id not in depths and neighbor_id not in frontier_set:
                frontier_set.add(neighbor_id)
                frontier.append(neighbor_id)

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
        truncated=bool(frontier),
        frontier=frontier,
    )
