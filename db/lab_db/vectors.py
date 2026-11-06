"""PgVectorStore core (SPEC 12.4, R15, T53-T57).

Writes and queries are checked against the collection's embedding space
(model, dimension, preprocessing and task pair are all inside the space's
config hash). Items are written only while a collection is building; it is
published (``ready``) only when the caller's manifest matches what is stored.
Queries are scoped to one project, one purpose and, for methods, one method
revision, so a participant query can never reach the system's
benchmark-generation collection or another method's collection.

Every query is recorded in ``vector_queries`` with the mode actually used.
An HNSW query is checked with EXPLAIN: if PostgreSQL did not use the index,
the call fails instead of labelling an exact scan as ANN. A filtered ANN
query that returns fewer than k rows records the shortfall and, only when
asked, falls back to an exact scan, which is recorded as a fallback.

Index DDL needs the owner (migration) role; the application role can write
items and query but not create indexes.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

Metric = Literal["cosine", "l2", "inner_product"]
Purpose = Literal["benchmark_generation", "method"]
_OPS = {"cosine": ("<=>", "vector_cosine_ops"), "l2": ("<->", "vector_l2_ops"),
        "inner_product": ("<#>", "vector_ip_ops")}


class VectorRejected(ValueError):
    """The vector itself is unusable: NaN/Infinity, wrong dimension, or a
    zero vector under cosine (T53)."""


class SpaceMismatch(ValueError):
    """Caller's embedder is not the collection's embedding space (T53)."""


class ContentConflict(ValueError):
    """Same (collection, item) retried with different content (T54)."""


class NotReady(RuntimeError):
    """Collection is not published; half-built indexes are not queryable (T54)."""


class ManifestMismatch(RuntimeError):
    pass


class IndexNotUsed(RuntimeError):
    """An ANN query was planned without the ANN index; refusing to mislabel it."""


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def space_config_hash(provider: str, model: str, model_revision: str, dimension: int, normalization: str,
                      document_task: str, query_task: str, preprocess_version: str, metric: str) -> str:
    return hashlib.sha256(_canon(dict(
        provider=provider, model=model, model_revision=model_revision, dimension=dimension,
        normalization=normalization, document_task=document_task, query_task=query_task,
        preprocess_version=preprocess_version, metric=metric))).hexdigest()


def create_space(conn: psycopg.Connection, project_id: uuid.UUID, *, provider: str, model: str,
                 model_revision: str, dimension: int, normalization: str, document_task: str,
                 query_task: str, preprocess_version: str, metric: Metric) -> tuple[uuid.UUID, str]:
    """Register (or find) an embedding space; returns (space_id, config_hash)."""
    if isinstance(dimension, bool) or not isinstance(dimension, int):
        raise ValueError("dimension must be an int")
    cfg = space_config_hash(provider, model, model_revision, dimension, normalization,
                            document_task, query_task, preprocess_version, metric)
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO embedding_spaces (id, project_id, provider, model, model_revision, dimension,
                   normalization, document_task, query_task, preprocess_version, metric, config_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (project_id, config_hash) DO NOTHING RETURNING id""",
            (uuid.uuid4(), project_id, provider, model, model_revision, dimension, normalization,
             document_task, query_task, preprocess_version, metric, cfg)).fetchone()
        if row is None:
            row = conn.execute("SELECT id FROM embedding_spaces WHERE project_id = %s AND config_hash = %s",
                               (project_id, cfg)).fetchone()
    return row[0], cfg


def create_collection(conn: psycopg.Connection, project_id: uuid.UUID, *, purpose: Purpose,
                      method_revision: Optional[str], space_id: uuid.UUID, document_hashes: Sequence[str],
                      operation_id: uuid.UUID) -> uuid.UUID:
    cid = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            """INSERT INTO vector_collections (id, project_id, purpose, method_revision, embedding_space_id,
                   document_hashes, created_by_operation_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (cid, project_id, purpose, method_revision, space_id, sorted(set(document_hashes)), operation_id))
    return cid


@dataclass(frozen=True)
class Item:
    item_id: str
    content_hash: str
    embedding: Sequence[float]
    document_sha256: str
    regions: Sequence[dict] = field(default_factory=tuple)


def _check_vector(vec: Sequence[float], dimension: int, metric: str, what: str) -> str:
    if isinstance(vec, (str, bytes)) or not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in vec):
        raise VectorRejected(f"{what}: vector must be a sequence of numbers")
    if len(vec) != dimension:
        raise VectorRejected(f"{what}: dimension {len(vec)} != space dimension {dimension}")
    if not all(math.isfinite(x) for x in vec):
        raise VectorRejected(f"{what}: NaN or Infinity is not allowed")
    if metric == "cosine" and not any(x != 0 for x in vec):
        raise VectorRejected(f"{what}: zero vector has no cosine distance")
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _collection(conn, collection_id, *, lock: bool = False):
    row = conn.execute(
        f"""SELECT c.project_id, c.purpose, c.method_revision, c.status, s.dimension, s.metric, s.config_hash,
                   c.manifest_hash
            FROM vector_collections c JOIN embedding_spaces s ON s.id = c.embedding_space_id
            WHERE c.id = %s {"FOR UPDATE OF c" if lock else ""}""", (collection_id,)).fetchone()
    if row is None:
        raise LookupError("collection not found")
    keys = ("project_id", "purpose", "method_revision", "status", "dimension", "metric", "space_hash",
            "manifest_hash")
    return dict(zip(keys, row))


def upsert(conn: psycopg.Connection, project_id: uuid.UUID, collection_id: uuid.UUID, *,
           space_hash: str, items: Sequence[Item], operation_id: uuid.UUID) -> int:
    """Write one batch atomically. A retried batch is a no-op for items whose
    content hash matches; a different content hash for an existing item is a
    ContentConflict and rolls the whole batch back. Returns the number of
    newly inserted items."""
    with conn.transaction():
        c = _collection(conn, collection_id)
        if c["project_id"] != project_id:
            raise LookupError("collection not found")
        if space_hash != c["space_hash"]:
            raise SpaceMismatch("writer's embedding space differs from the collection's space")
        literals = [_check_vector(it.embedding, c["dimension"], c["metric"], f"item {it.item_id}") for it in items]
        if len({it.item_id for it in items}) != len(items):
            raise ValueError("duplicate item_id in batch")
        inserted = 0
        for it, lit in zip(items, literals):
            row = conn.execute(
                """INSERT INTO vector_items (project_id, collection_id, item_id, content_hash, embedding,
                       document_sha256, created_by_operation_id)
                   VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
                   ON CONFLICT (collection_id, item_id) DO NOTHING RETURNING item_id""",
                (project_id, collection_id, it.item_id, it.content_hash, lit, it.document_sha256,
                 operation_id)).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT content_hash FROM vector_items WHERE collection_id = %s AND item_id = %s",
                    (collection_id, it.item_id)).fetchone()[0]
                if existing != it.content_hash:
                    raise ContentConflict(f"item {it.item_id} already stored with different content")
                continue
            inserted += 1
            for k, region in enumerate(it.regions):
                conn.execute("INSERT INTO vector_item_regions VALUES (%s, %s, %s, %s)",
                             (collection_id, it.item_id, k, Jsonb(region)))
        return inserted


def manifest(conn: psycopg.Connection, collection_id: uuid.UUID) -> tuple[int, str]:
    rows = conn.execute("SELECT item_id, content_hash FROM vector_items WHERE collection_id = %s",
                        (collection_id,)).fetchall()
    return len(rows), manifest_hash_of((i, h) for i, h in rows)


def manifest_hash_of(pairs) -> str:
    body = "".join(f"{i}\t{h}\n" for i, h in sorted(pairs))
    return hashlib.sha256(body.encode()).hexdigest()


def publish(conn: psycopg.Connection, project_id: uuid.UUID, collection_id: uuid.UUID, *,
            expected_count: int, expected_manifest_hash: str) -> None:
    """building -> ready, only if the stored items are exactly the expected
    corpus (T54: no half-built index is ever published)."""
    with conn.transaction():
        c = _collection(conn, collection_id, lock=True)
        if c["project_id"] != project_id:
            raise LookupError("collection not found")
        if c["status"] != "building":
            raise NotReady(f"collection is {c['status']}, not building")
        count, mh = manifest(conn, collection_id)
        if (count, mh) != (expected_count, expected_manifest_hash):
            raise ManifestMismatch(f"stored {count} items / {mh}, expected {expected_count} / {expected_manifest_hash}")
        conn.execute("""UPDATE vector_collections SET status = 'ready', manifest_hash = %s, item_count = %s,
                           ready_at = now() WHERE id = %s""", (mh, count, collection_id))


def extension_version(conn) -> str:
    return conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]


def build_index(conn: psycopg.Connection, project_id: uuid.UUID, collection_id: uuid.UUID, *,
                mode: Literal["exact", "hnsw"], m: int = 16, ef_construction: int = 64,
                ef_search: int = 40) -> uuid.UUID:
    """Record an index revision for a published collection; for HNSW also
    build a partial expression index on that collection only."""
    for name, v, lo, hi in (("m", m, 2, 100), ("ef_construction", ef_construction, 4, 1000),
                            ("ef_search", ef_search, 1, 1000)):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{name} must be an int in [{lo}, {hi}]")
    if mode not in ("exact", "hnsw"):
        raise ValueError("mode must be exact or hnsw")
    rev = uuid.uuid4()
    with conn.transaction():
        c = _collection(conn, collection_id)
        if c["project_id"] != project_id:
            raise LookupError("collection not found")
        if c["status"] not in ("ready", "frozen"):
            raise NotReady("indexes are built on a published corpus only")
        index_name = None
        build_params: dict = {}
        query_params: dict = {}
        if mode == "hnsw":
            index_name = "vi_" + rev.hex
            build_params = {"m": m, "ef_construction": ef_construction}
            query_params = {"ef_search": ef_search}
            _, ops = _OPS[c["metric"]]
            conn.execute(sql.SQL(
                "CREATE INDEX {} ON vector_items USING hnsw ((embedding::vector({})) {}) "
                "WITH (m = {}, ef_construction = {}) WHERE collection_id = {}").format(
                sql.Identifier(index_name), sql.Literal(c["dimension"]), sql.SQL(ops),
                sql.Literal(m), sql.Literal(ef_construction), sql.Literal(str(collection_id))))
        conn.execute(
            """INSERT INTO vector_index_revisions (id, collection_id, mode, data_type, build_params, query_params,
                   extension_version, corpus_manifest_hash, index_name)
               VALUES (%s, %s, %s, 'vector', %s, %s, %s, %s, %s)""",
            (rev, collection_id, mode, Jsonb(build_params), Jsonb(query_params), extension_version(conn),
             c["manifest_hash"], index_name))
    return rev


@dataclass(frozen=True)
class Hit:
    rank: int
    item_id: str
    distance: float
    similarity: float   # higher is better (cosine: 1 - distance)


@dataclass(frozen=True)
class QueryResult:
    query_id: uuid.UUID
    hits: tuple[Hit, ...]
    mode_requested: str
    mode_used: str
    shortfall: int          # k minus rows returned by the requested mode
    fallback: Optional[str]


def _similarity(metric: str, d: float) -> float:
    # cosine: distance = 1 - cos. l2: smaller is better. pgvector's <#> is
    # the negative inner product, so -d is the inner product.
    return 1.0 - d if metric == "cosine" else -d


def _run(conn, c, collection_id, lit, k, docs, index_name=None):
    op, _ = _OPS[c["metric"]]
    dim = sql.Literal(c["dimension"])
    if index_name is None:
        expr = sql.SQL("embedding {} {}::vector").format(sql.SQL(op), sql.Literal(lit))
    else:
        expr = sql.SQL("(embedding::vector({})) {} {}::vector({})").format(dim, sql.SQL(op), sql.Literal(lit), dim)
    where = sql.SQL("collection_id = {}").format(sql.Literal(str(collection_id)))
    if docs is not None:
        where = sql.SQL("{} AND document_sha256 = ANY({})").format(where, sql.Literal(sorted(docs)))
    order = sql.SQL("d, item_id") if index_name is None else sql.SQL("d")
    q = sql.SQL("SELECT item_id, {} AS d FROM vector_items WHERE {} ORDER BY {} LIMIT {}").format(
        expr, where, order, sql.Literal(k))
    if index_name is not None:
        plan = json.dumps(conn.execute(sql.SQL("EXPLAIN (FORMAT JSON) ") + q).fetchone()[0])
        if index_name not in plan:
            raise IndexNotUsed(f"planner did not use {index_name}: {plan[:2000]}")
    rows = conn.execute(q).fetchall()
    # Stable tie order for both modes: (distance, item_id).
    return sorted(((r[0], float(r[1])) for r in rows), key=lambda t: (t[1], t[0]))


def search(conn: psycopg.Connection, project_id: uuid.UUID, collection_id: uuid.UUID, *,
           purpose: Purpose, method_revision: Optional[str], query_space_hash: str,
           vector: Sequence[float], k: int, index_revision_id: uuid.UUID,
           document_filter: Optional[set[str]] = None, exact_fallback: bool = False,
           trace_id: Optional[str] = None) -> QueryResult:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 1000:
        raise ValueError("k must be an int in [1, 1000]")
    started = time.perf_counter()
    with conn.transaction():
        c = _collection(conn, collection_id)
        # One indistinguishable error for "not yours": other project, other
        # purpose (e.g. a participant asking for the generation collection)
        # or another method's collection (T55).
        if (c["project_id"] != project_id or c["purpose"] != purpose
                or (purpose == "method" and c["method_revision"] != method_revision)):
            raise LookupError("collection not found")
        if c["status"] not in ("ready", "frozen"):
            raise NotReady(f"collection is {c['status']}")
        if query_space_hash != c["space_hash"]:
            raise SpaceMismatch("query embedding space differs from the collection's space")
        lit = _check_vector(vector, c["dimension"], c["metric"], "query")
        rev = conn.execute("SELECT mode, query_params, index_name FROM vector_index_revisions "
                           "WHERE id = %s AND collection_id = %s", (index_revision_id, collection_id)).fetchone()
        if rev is None:
            raise LookupError("index revision not found for this collection")
        mode, qparams, index_name = rev
        docs = None if document_filter is None else set(document_filter)
        if mode == "hnsw":
            conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(int(qparams["ef_search"]))))
            # Steer the planner to the ANN index (a small table would otherwise
            # be scanned via the primary key and sorted); EXPLAIN in _run still
            # verifies the index is really used.
            for knob in ("enable_seqscan", "enable_sort", "enable_bitmapscan"):
                conn.execute(f"SET LOCAL {knob} = off")
            rows = _run(conn, c, collection_id, lit, k, docs, index_name)
            for knob in ("enable_seqscan", "enable_sort", "enable_bitmapscan"):
                conn.execute(f"RESET {knob}")
        else:
            rows = _run(conn, c, collection_id, lit, k, docs)
        shortfall = k - len(rows)
        mode_used, fallback = mode, None
        if mode != "exact" and shortfall > 0 and exact_fallback:
            rows = _run(conn, c, collection_id, lit, k, docs)
            mode_used, fallback = "exact", "exact_after_shortfall"
        hits = tuple(Hit(i + 1, item, d, _similarity(c["metric"], d)) for i, (item, d) in enumerate(rows))
        elapsed = (time.perf_counter() - started) * 1000
        qid = uuid.uuid4()
        conn.execute(
            """INSERT INTO vector_queries (id, project_id, collection_id, index_revision_id, query_hash,
                   query_space_hash, k, filter, metric, mode_requested, mode_used, fallback, shortfall,
                   results, elapsed_ms, trace_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (qid, project_id, collection_id, index_revision_id, hashlib.sha256(lit.encode()).hexdigest(),
             query_space_hash, k, Jsonb({"document_sha256": sorted(docs)} if docs is not None else {}),
             c["metric"], mode, mode_used, fallback, shortfall,
             Jsonb([{"rank": h.rank, "item_id": h.item_id, "distance": h.distance, "similarity": h.similarity}
                    for h in hits]), elapsed, trace_id))
    return QueryResult(qid, hits, mode, mode_used, shortfall, fallback)
