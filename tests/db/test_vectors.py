"""Real PostgreSQL + pgvector tests for the vector store (SPEC 12.4, R15, T53-T57, F07 cases)."""
import math
import random
import threading
import time
import unittest
import uuid
import psycopg
from psycopg.types.json import Jsonb
from lab_db import vectors as V
from tests.db.base import FreshDatabase, h

D1 = h("doc1")
D2 = h("doc2")


class TestVectors(FreshDatabase):
    """SPEC 12.4, R15, T53-T57 vector store integration test suite."""

    def connect(self, **kw):
        conn = super().connect(**kw)
        self.addCleanup(conn.close)
        return conn

    def op(self, conn, project_id, actor_id) -> uuid.UUID:
        """Helper to insert an operation record."""
        oid = uuid.uuid4()
        conn.execute(
            "INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s,%s,'vector.write',%s,'running')",
            (oid, project_id, actor_id),
        )
        conn.commit()
        return oid

    def setup_collection(
        self,
        conn,
        dim=3,
        metric="cosine",
        purpose="method",
        method_revision="m@1",
        docs=(D1,),
        project_id=None,
        actor_id=None,
    ):
        """Setup helper: create space and collection, returning 6-tuple identity."""
        if project_id is None or actor_id is None:
            p, a = self.make_project(conn, role="owner")
            project_id = project_id or p
            actor_id = actor_id or a
        op_id = self.op(conn, project_id, actor_id)
        space_id, space_hash = V.create_space(
            conn,
            project_id,
            provider="test",
            model="toy",
            model_revision="1",
            dimension=dim,
            normalization="none",
            document_task="doc",
            query_task="query",
            preprocess_version="p1",
            metric=metric,
        )
        collection_id = V.create_collection(
            conn,
            project_id,
            purpose=purpose,
            method_revision=method_revision,
            space_id=space_id,
            document_hashes=docs,
            operation_id=op_id,
        )
        conn.commit()
        return (project_id, actor_id, op_id, collection_id, space_id, space_hash)

    def test_rejects_bad_vectors_without_polluting(self):
        """SPEC 12.4, T53: Invalid vectors and mismatched spaces are rejected without database pollution."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, space_id, space_hash = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,)
        )

        # Dimension mismatch (2 vs 3)
        bad_dim = V.Item(item_id="bad_dim", content_hash=h("c_dim"), embedding=[1, 2], document_sha256=D1)
        with self.assertRaises(V.VectorRejected):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[bad_dim], operation_id=op_id)

        # NaN vector
        bad_nan = V.Item(item_id="bad_nan", content_hash=h("c_nan"), embedding=[float("nan"), 0.0, 0.0], document_sha256=D1)
        with self.assertRaises(V.VectorRejected):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[bad_nan], operation_id=op_id)

        # Infinity vector
        bad_inf = V.Item(item_id="bad_inf", content_hash=h("c_inf"), embedding=[float("inf"), 0.0, 0.0], document_sha256=D1)
        with self.assertRaises(V.VectorRejected):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[bad_inf], operation_id=op_id)

        # Zero vector under cosine distance
        bad_zero = V.Item(item_id="bad_zero", content_hash=h("c_zero"), embedding=[0.0, 0.0, 0.0], document_sha256=D1)
        with self.assertRaises(V.VectorRejected):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[bad_zero], operation_id=op_id)

        # Mismatched space hash
        good_item = V.Item(item_id="probe", content_hash=h("c_probe"), embedding=[1.0, 0.0, 0.0], document_sha256=D1)
        with self.assertRaises(V.SpaceMismatch):
            V.upsert(conn, p, col_id, space_hash="0" * 64, items=[good_item], operation_id=op_id)

        # Verify no items polluted the database
        count = conn.execute("SELECT count(*) FROM vector_items WHERE collection_id = %s", (col_id,)).fetchone()[0]
        self.assertEqual(count, 0)

        # Direct SQL INSERT bypassing Python with 2-dim vector
        with self.assertRaises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO vector_items (project_id, collection_id, item_id, content_hash, embedding,
                       document_sha256, created_by_operation_id)
                   VALUES (%s, %s, %s, %s, %s::vector, %s, %s)""",
                (p, col_id, "sql_bad_dim", h("sql1"), "[1,2]", D1, op_id),
            )
        conn.rollback()

        # Direct SQL INSERT bypassing Python with zero vector under cosine
        with self.assertRaises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO vector_items (project_id, collection_id, item_id, content_hash, embedding,
                       document_sha256, created_by_operation_id)
                   VALUES (%s, %s, %s, %s, %s::vector, %s, %s)""",
                (p, col_id, "sql_bad_zero", h("sql2"), "[0,0,0]", D1, op_id),
            )
        conn.rollback()

        # Query side: publish an empty-valid collection with one good item first
        valid_item = V.Item(item_id="valid_1", content_hash=h("c_val"), embedding=[1.0, 0.0, 0.0], document_sha256=D1)
        V.upsert(conn, p, col_id, space_hash=space_hash, items=[valid_item], operation_id=op_id)
        cnt, mh = V.manifest(conn, col_id)
        V.publish(conn, p, col_id, expected_count=cnt, expected_manifest_hash=mh)
        idx_id = V.build_index(conn, p, col_id, mode="exact")
        conn.commit()

        # Search with NaN vector -> VectorRejected
        with self.assertRaises(V.VectorRejected):
            V.search(
                conn, p, col_id,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash,
                vector=[float("nan"), 0.0, 0.0],
                k=1,
                index_revision_id=idx_id,
            )

        # Search with wrong query_space_hash -> SpaceMismatch
        with self.assertRaises(V.SpaceMismatch):
            V.search(
                conn, p, col_id,
                purpose="method",
                method_revision="m@1",
                query_space_hash="0" * 64,
                vector=[1.0, 0.0, 0.0],
                k=1,
                index_revision_id=idx_id,
            )

    def test_batch_retry_conflict_and_ready_gate(self):
        """SPEC 12.4, T54: Batch retries are idempotent, content conflicts rollback, and publish gates mutations."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, space_id, space_hash = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,)
        )

        item_a = V.Item(item_id="a", content_hash=h("content_a"), embedding=[1.0, 0.0, 0.0], document_sha256=D1)
        item_b = V.Item(item_id="b", content_hash=h("content_b"), embedding=[0.0, 1.0, 0.0], document_sha256=D1)

        # Upsert initial batch [a, b] -> 2 new items
        inserted = V.upsert(conn, p, col_id, space_hash=space_hash, items=[item_a, item_b], operation_id=op_id)
        self.assertEqual(inserted, 2)
        conn.commit()

        # Same batch again returns 0 and count stays 2
        retry_inserted = V.upsert(conn, p, col_id, space_hash=space_hash, items=[item_a, item_b], operation_id=op_id)
        self.assertEqual(retry_inserted, 0)
        conn.commit()
        cnt, _ = V.manifest(conn, col_id)
        self.assertEqual(cnt, 2)

        # Batch [c(new), a(with different content_hash)] raises ContentConflict and c is not stored
        item_c = V.Item(item_id="c", content_hash=h("content_c"), embedding=[0.0, 0.0, 1.0], document_sha256=D1)
        item_a_diff = V.Item(item_id="a", content_hash=h("content_a_diff"), embedding=[1.0, 0.0, 0.0], document_sha256=D1)
        with self.assertRaises(V.ContentConflict):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[item_c, item_a_diff], operation_id=op_id)

        cnt, _ = V.manifest(conn, col_id)
        self.assertEqual(cnt, 2)
        row = conn.execute("SELECT 1 FROM vector_items WHERE collection_id = %s AND item_id = 'c'", (col_id,)).fetchone()
        self.assertIsNone(row)

        # Search before publish raises NotReady before revision lookup
        with self.assertRaises(V.NotReady):
            V.search(
                conn, p, col_id,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash,
                vector=[1.0, 0.0, 0.0],
                k=1,
                index_revision_id=uuid.uuid4(),
            )

        # Publish with wrong expected_count raises ManifestMismatch and status stays building
        correct_mh = V.manifest_hash_of([("a", item_a.content_hash), ("b", item_b.content_hash)])
        with self.assertRaises(V.ManifestMismatch):
            V.publish(conn, p, col_id, expected_count=3, expected_manifest_hash=correct_mh)
        status = conn.execute("SELECT status FROM vector_collections WHERE id = %s", (col_id,)).fetchone()[0]
        self.assertEqual(status, "building")

        # Publish with correct (count, manifest_hash) succeeds; status 'ready'
        V.publish(conn, p, col_id, expected_count=2, expected_manifest_hash=correct_mh)
        conn.commit()
        status = conn.execute("SELECT status FROM vector_collections WHERE id = %s", (col_id,)).fetchone()[0]
        self.assertEqual(status, "ready")

        # After ready, upsert of a new item raises IntegrityConstraintViolation
        item_d = V.Item(item_id="d", content_hash=h("content_d"), embedding=[0.0, 0.0, 1.0], document_sha256=D1)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            V.upsert(conn, p, col_id, space_hash=space_hash, items=[item_d], operation_id=op_id)
        conn.rollback()

        # After ready, direct insert of a region also raises IntegrityConstraintViolation
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("INSERT INTO vector_item_regions VALUES (%s, %s, %s, %s)",
                         (col_id, "a", 0, Jsonb({"page": 1})))
        conn.rollback()

        # UPDATE vector_collections SET status='building' raises IntegrityConstraintViolation
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE vector_collections SET status = 'building' WHERE id = %s", (col_id,))
        conn.rollback()

    def test_isolation_between_methods_projects_and_generation(self):
        """SPEC 12.4, T55: Collections are strictly isolated across methods, projects, and benchmark generation."""
        conn = self.connect(autocommit=False)
        p, a = self.make_project(conn, role="owner")

        # Collection A: purpose="method", method_revision="m@1"
        _, _, op_a, col_a, _, space_hash_a = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,), project_id=p, actor_id=a
        )
        it_a = V.Item(item_id="item_a", content_hash=h("c_a"), embedding=[1.0, 0.0, 0.0], document_sha256=D1)
        V.upsert(conn, p, col_a, space_hash=space_hash_a, items=[it_a], operation_id=op_a)
        cnt_a, mh_a = V.manifest(conn, col_a)
        V.publish(conn, p, col_a, expected_count=cnt_a, expected_manifest_hash=mh_a)
        idx_a = V.build_index(conn, p, col_a, mode="exact")

        # Collection B: purpose="method", method_revision="m@2"
        _, _, op_b, col_b, _, space_hash_b = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@2", docs=(D1,), project_id=p, actor_id=a
        )
        it_b = V.Item(item_id="item_b", content_hash=h("c_b"), embedding=[0.0, 1.0, 0.0], document_sha256=D1)
        V.upsert(conn, p, col_b, space_hash=space_hash_b, items=[it_b], operation_id=op_b)
        cnt_b, mh_b = V.manifest(conn, col_b)
        V.publish(conn, p, col_b, expected_count=cnt_b, expected_manifest_hash=mh_b)
        idx_b = V.build_index(conn, p, col_b, mode="exact")

        # Collection G: purpose="benchmark_generation", method_revision=None
        _, _, op_g, col_g, _, space_hash_g = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="benchmark_generation", method_revision=None, docs=(D1,), project_id=p, actor_id=a
        )
        it_g = V.Item(item_id="item_g", content_hash=h("c_g"), embedding=[0.0, 0.0, 1.0], document_sha256=D1)
        V.upsert(conn, p, col_g, space_hash=space_hash_g, items=[it_g], operation_id=op_g)
        cnt_g, mh_g = V.manifest(conn, col_g)
        V.publish(conn, p, col_g, expected_count=cnt_g, expected_manifest_hash=mh_g)
        idx_g = V.build_index(conn, p, col_g, mode="exact")
        conn.commit()

        # Search A with purpose="method", method_revision="m@2" -> LookupError
        with self.assertRaises(LookupError):
            V.search(
                conn, p, col_a,
                purpose="method",
                method_revision="m@2",
                query_space_hash=space_hash_a,
                vector=[1.0, 0.0, 0.0],
                k=5,
                index_revision_id=idx_a,
            )

        # Search G with purpose="method", method_revision="m@1" -> LookupError
        with self.assertRaises(LookupError):
            V.search(
                conn, p, col_g,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash_g,
                vector=[0.0, 0.0, 1.0],
                k=5,
                index_revision_id=idx_g,
            )

        # Search A from a different project_id -> LookupError
        p_other, _ = self.make_project(conn, role="owner")
        with self.assertRaises(LookupError):
            V.search(
                conn, p_other, col_a,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash_a,
                vector=[1.0, 0.0, 0.0],
                k=5,
                index_revision_id=idx_a,
            )

        # Correct search returns only A's item
        res_a = V.search(
            conn, p, col_a,
            purpose="method",
            method_revision="m@1",
            query_space_hash=space_hash_a,
            vector=[1.0, 0.0, 0.0],
            k=5,
            index_revision_id=idx_a,
        )
        self.assertEqual(len(res_a.hits), 1)
        self.assertEqual(res_a.hits[0].item_id, "item_a")

        # Item outside collection document scope is rejected by upsert trigger
        _, _, op_scope, col_scope, _, space_hash_scope = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,), project_id=p, actor_id=a
        )
        bad_doc_item = V.Item(
            item_id="bad_doc",
            content_hash=h("c_bad_doc"),
            embedding=[1.0, 0.0, 0.0],
            document_sha256=D2,
        )
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            V.upsert(conn, p, col_scope, space_hash=space_hash_scope, items=[bad_doc_item], operation_id=op_scope)
        conn.rollback()

    def test_exact_cosine_matches_hand_computation(self):
        """SPEC 12.4, T56: Exact cosine distance matches hand computation with deterministic tie order."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, space_id, space_hash = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,)
        )

        raw_items = [
            ("a", [1.0, 0.0, 0.0]),
            ("b", [0.0, 1.0, 0.0]),
            ("c", [1.0, 1.0, 0.0]),
            ("d", [-1.0, 0.0, 0.0]),
            ("e", [2.0, 0.0, 0.0]),
        ]
        items = [
            V.Item(item_id=iid, content_hash=h(f"content_{iid}"), embedding=vec, document_sha256=D1)
            for iid, vec in raw_items
        ]
        V.upsert(conn, p, col_id, space_hash=space_hash, items=items, operation_id=op_id)
        cnt, mh = V.manifest(conn, col_id)
        self.assertEqual(cnt, 5)
        V.publish(conn, p, col_id, expected_count=5, expected_manifest_hash=mh)
        idx_id = V.build_index(conn, p, col_id, mode="exact")
        conn.commit()

        res = V.search(
            conn, p, col_id,
            purpose="method",
            method_revision="m@1",
            query_space_hash=space_hash,
            vector=[1.0, 0.0, 0.0],
            k=5,
            index_revision_id=idx_id,
        )

        self.assertEqual(res.mode_requested, "exact")
        self.assertEqual(res.mode_used, "exact")
        self.assertEqual(res.shortfall, 0)
        self.assertIsNone(res.fallback)
        self.assertEqual(len(res.hits), 5)

        expected = [
            ("a", 0.0),
            ("e", 0.0),
            ("c", 1.0 - 1.0 / math.sqrt(2)),
            ("b", 1.0),
            ("d", 2.0),
        ]
        for rank_idx, (hit, (exp_id, exp_dist)) in enumerate(zip(res.hits, expected), start=1):
            self.assertEqual(hit.rank, rank_idx)
            self.assertEqual(hit.item_id, exp_id)
            self.assertLessEqual(abs(hit.distance - exp_dist), 1e-6)
            self.assertEqual(hit.similarity, 1.0 - hit.distance)

        row = conn.execute(
            "SELECT k, jsonb_array_length(results), mode_used FROM vector_queries WHERE id = %s",
            (res.query_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 5)
        self.assertEqual(row[1], 5)
        self.assertEqual(row[2], "exact")

    def test_hnsw_mode_recall_shortfall_and_fallback(self):
        """SPEC 12.4, T57: HNSW recall, distance parity with exact scan, and shortfall fallback."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, space_id, space_hash = self.setup_collection(
            conn, dim=8, metric="cosine", purpose="method", method_revision="m@1", docs=(D1, D2)
        )

        rng = random.Random(5)
        items = []
        for i in range(300):
            doc = D1 if i < 297 else D2
            vec = [rng.gauss(0.0, 1.0) for _ in range(8)]
            items.append(
                V.Item(
                    item_id=f"item_{i:03d}",
                    content_hash=h(f"content_{i}"),
                    embedding=vec,
                    document_sha256=doc,
                )
            )

        inserted = V.upsert(conn, p, col_id, space_hash=space_hash, items=items, operation_id=op_id)
        self.assertEqual(inserted, 300)
        cnt, mh = V.manifest(conn, col_id)
        self.assertEqual(cnt, 300)
        V.publish(conn, p, col_id, expected_count=300, expected_manifest_hash=mh)

        exact_rev = V.build_index(conn, p, col_id, mode="exact")
        hnsw_rev = V.build_index(conn, p, col_id, mode="hnsw", m=16, ef_construction=64, ef_search=40)
        conn.commit()

        # 10 random queries to evaluate recall@10 and distance accuracy
        recalls = []
        for q_idx in range(10):
            q_vec = [rng.gauss(0.0, 1.0) for _ in range(8)]

            # Query exact with k=300 to retrieve all items and true distances
            exact_all = V.search(
                conn, p, col_id,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash,
                vector=q_vec,
                k=300,
                index_revision_id=exact_rev,
            )
            exact_top10_ids = {h.item_id for h in exact_all.hits[:10]}
            exact_dist_map = {h.item_id: h.distance for h in exact_all.hits}

            # Query HNSW with k=10
            hnsw_res = V.search(
                conn, p, col_id,
                purpose="method",
                method_revision="m@1",
                query_space_hash=space_hash,
                vector=q_vec,
                k=10,
                index_revision_id=hnsw_rev,
            )
            self.assertEqual(hnsw_res.mode_used, "hnsw")
            self.assertEqual(len(hnsw_res.hits), 10)
            for hit in hnsw_res.hits:
                self.assertIn(hit.item_id, exact_dist_map)
                self.assertLessEqual(abs(hit.distance - exact_dist_map[hit.item_id]), 1e-6)

            hnsw_ids = {h.item_id for h in hnsw_res.hits}
            recalls.append(len(hnsw_ids & exact_top10_ids) / 10.0)

        mean_recall = sum(recalls) / len(recalls)
        print(f"HNSW mean recall@10: {mean_recall:.4f}")
        self.assertGreaterEqual(mean_recall, 0.8)

        # Filtered query with document_filter={D2}, k=10
        filter_vec = [rng.gauss(0.0, 1.0) for _ in range(8)]

        # HNSW without fallback: shortfall >= 7, mode_used='hnsw', fallback=None
        res_nofb = V.search(
            conn, p, col_id,
            purpose="method",
            method_revision="m@1",
            query_space_hash=space_hash,
            vector=filter_vec,
            k=10,
            index_revision_id=hnsw_rev,
            document_filter={D2},
            exact_fallback=False,
        )
        self.assertEqual(res_nofb.mode_used, "hnsw")
        self.assertGreaterEqual(res_nofb.shortfall, 7)
        self.assertIsNone(res_nofb.fallback)

        # With exact_fallback=True: fallback to exact, returns all 3 D2 items
        res_fb = V.search(
            conn, p, col_id,
            purpose="method",
            method_revision="m@1",
            query_space_hash=space_hash,
            vector=filter_vec,
            k=10,
            index_revision_id=hnsw_rev,
            document_filter={D2},
            exact_fallback=True,
        )
        self.assertEqual(res_fb.mode_used, "exact")
        self.assertEqual(res_fb.fallback, "exact_after_shortfall")
        self.assertEqual(len(res_fb.hits), 3)
        expected_d2_ids = {f"item_{i:03d}" for i in range(297, 300)}
        self.assertEqual({h.item_id for h in res_fb.hits}, expected_d2_ids)

        row = conn.execute(
            "SELECT mode_requested, mode_used, fallback FROM vector_queries WHERE id = %s",
            (res_fb.query_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "hnsw")
        self.assertEqual(row[1], "exact")
        self.assertEqual(row[2], "exact_after_shortfall")

    def test_same_dimension_other_model_and_cross_project_writes(self):
        """F07: a space with the same dimension but another model is still a
        different space; a project cannot write into another project's collection."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, _, space_hash = self.setup_collection(conn, dim=3)
        _, other_hash = V.create_space(conn, p, provider="test", model="other-toy", model_revision="1", dimension=3,
                                       normalization="none", document_task="doc", query_task="query",
                                       preprocess_version="p1", metric="cosine")
        self.assertNotEqual(other_hash, space_hash)
        item = V.Item(item_id="x", content_hash=h("x"), embedding=[1.0, 2.0, 3.0], document_sha256=D1)
        with self.assertRaises(V.SpaceMismatch):
            V.upsert(conn, p, col_id, space_hash=other_hash, items=[item], operation_id=op_id)
        p2, a2 = self.make_project(conn, role="owner")
        op2 = self.op(conn, p2, a2)
        with self.assertRaises(LookupError):
            V.upsert(conn, p2, col_id, space_hash=space_hash, items=[item], operation_id=op2)
        # Same config in the same project resolves to the same space (idempotent registration).
        again_id, again_hash = V.create_space(conn, p, provider="test", model="toy", model_revision="1", dimension=3,
                                              normalization="none", document_task="doc", query_task="query",
                                              preprocess_version="p1", metric="cosine")
        self.assertEqual(again_hash, space_hash)
        self.assertEqual(conn.execute("SELECT count(*) FROM vector_items WHERE collection_id = %s",
                                      (col_id,)).fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE embedding_spaces SET model = 'changed' WHERE id = %s", (again_id,))
        conn.rollback()

    def test_region_insert_and_publish_synchronize_on_collection_lock(self):
        """SPEC 12.4, R17, T54-T57 / R30, T102: vector_item_regions insert and publish
        synchronize on the same vector_collections row lock. Covering both interleavings
        with two real PostgreSQL connections, verifying manifest and ready source mapping."""
        conn = self.connect(autocommit=False)
        p, a, op_id, col_id, space_id, space_hash = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,)
        )
        reg0 = {"page": 1, "bbox": [0, 0, 100, 100]}
        reg1 = {"page": 2, "bbox": [10, 10, 50, 50]}
        it1 = V.Item(item_id="item_1", content_hash=h("c_1"), embedding=[1.0, 0.0, 0.0], document_sha256=D1, regions=(reg0,))
        V.upsert(conn, p, col_id, space_hash=space_hash, items=[it1], operation_id=op_id)
        cnt, mh = V.manifest(conn, col_id)
        self.assertEqual(cnt, 1)
        conn.commit()

        # --- Interleaving 1: Publish locks collection first; region write waits on the
        # row lock, then is rejected once publish commits ready. ---
        conn_pub = self.connect(autocommit=True)
        conn_writer = self.connect(autocommit=True)

        pub_locked = threading.Event()
        pub_release = threading.Event()
        pub_done = threading.Event()
        pub_err = []

        def publisher_worker():
            try:
                with conn_pub.transaction():
                    V._collection(conn_pub, col_id, lock=True)
                    pub_locked.set()
                    if not pub_release.wait(timeout=10.0):
                        raise TimeoutError("publisher release timed out")
                    V.publish(conn_pub, p, col_id, expected_count=cnt, expected_manifest_hash=mh)
                pub_done.set()
            except Exception as e:
                pub_err.append(e)

        writer_started = threading.Event()
        writer_err = []
        writer_done = threading.Event()

        def writer_worker():
            try:
                with conn_writer.transaction():
                    writer_started.set()
                    conn_writer.execute(
                        "INSERT INTO vector_item_regions VALUES (%s, %s, %s, %s)",
                        (col_id, "item_1", 1, Jsonb(reg1)),
                    )
                writer_done.set()
            except Exception as e:
                writer_err.append(e)

        t_pub = threading.Thread(target=publisher_worker, name="interleaving1-publisher")
        t_writer = threading.Thread(target=writer_worker, name="interleaving1-writer")
        try:
            t_pub.start()
            self.assertTrue(pub_locked.wait(timeout=5.0), "publisher failed to acquire row lock")

            t_writer.start()
            self.assertTrue(writer_started.wait(timeout=5.0))
            time.sleep(0.2)
            self.assertTrue(t_writer.is_alive(), "writer must block on collection row lock")
            self.assertFalse(writer_done.is_set())

            pub_release.set()
            t_pub.join(timeout=10.0)
            self.assertFalse(t_pub.is_alive())
            self.assertEqual(pub_err, [])
            self.assertTrue(pub_done.is_set())

            t_writer.join(timeout=10.0)
            self.assertFalse(t_writer.is_alive())
            self.assertFalse(writer_done.is_set())
            self.assertEqual(len(writer_err), 1)
            self.assertIsInstance(writer_err[0], psycopg.errors.IntegrityConstraintViolation)
        finally:
            pub_release.set()
            t_pub.join(timeout=10.0)
            t_writer.join(timeout=10.0)

        with self.connect() as verify_conn:
            st, count_db, mh_db = verify_conn.execute(
                "SELECT status, item_count, manifest_hash FROM vector_collections WHERE id = %s",
                (col_id,),
            ).fetchone()
            self.assertEqual(st, "ready")
            self.assertEqual(count_db, 1)
            self.assertEqual(mh_db, mh)

            # Ready mapping contains only ordinal 0; ordinal 1 was rejected
            regions = verify_conn.execute(
                "SELECT item_id, ordinal, region FROM vector_item_regions WHERE collection_id = %s ORDER BY ordinal",
                (col_id,),
            ).fetchall()
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0], ("item_1", 0, reg0))

            cnt_rec, mh_rec = V.manifest(verify_conn, col_id)
            self.assertEqual(cnt_rec, 1)
            self.assertEqual(mh_rec, mh)

        # --- Interleaving 2: Region write locks collection first; publish waits on the
        # row lock, then proceeds only after region write commits. ---
        p2, a2, op2, col2, space_id2, space_hash2 = self.setup_collection(
            conn, dim=3, metric="cosine", purpose="method", method_revision="m@1", docs=(D1,)
        )
        reg0_2 = {"page": 1, "bbox": [0, 0, 100, 100]}
        reg1_2 = {"page": 2, "bbox": [10, 10, 50, 50]}
        it2 = V.Item(item_id="item_2", content_hash=h("c_2"), embedding=[0.0, 1.0, 0.0], document_sha256=D1, regions=(reg0_2,))
        V.upsert(conn, p2, col2, space_hash=space_hash2, items=[it2], operation_id=op2)
        cnt2, mh2 = V.manifest(conn, col2)
        self.assertEqual(cnt2, 1)
        conn.commit()

        conn_pub2 = self.connect(autocommit=True)
        conn_writer2 = self.connect(autocommit=True)

        writer2_locked = threading.Event()
        writer2_release = threading.Event()
        writer2_done = threading.Event()
        writer2_err = []

        def writer2_worker():
            try:
                with conn_writer2.transaction():
                    conn_writer2.execute(
                        "INSERT INTO vector_item_regions VALUES (%s, %s, %s, %s)",
                        (col2, "item_2", 1, Jsonb(reg1_2)),
                    )
                    writer2_locked.set()
                    if not writer2_release.wait(timeout=10.0):
                        raise TimeoutError("writer2 release timed out")
                writer2_done.set()
            except Exception as e:
                writer2_err.append(e)

        pub2_started = threading.Event()
        pub2_done = threading.Event()
        pub2_err = []

        def pub2_worker():
            try:
                pub2_started.set()
                V.publish(conn_pub2, p2, col2, expected_count=cnt2, expected_manifest_hash=mh2)
                pub2_done.set()
            except Exception as e:
                pub2_err.append(e)

        t_writer2 = threading.Thread(target=writer2_worker, name="interleaving2-writer")
        t_pub2 = threading.Thread(target=pub2_worker, name="interleaving2-publisher")
        try:
            t_writer2.start()
            self.assertTrue(writer2_locked.wait(timeout=5.0), "writer2 failed to acquire row lock")

            t_pub2.start()
            self.assertTrue(pub2_started.wait(timeout=5.0))
            time.sleep(0.2)
            self.assertTrue(t_pub2.is_alive(), "publisher must block on collection row lock")
            self.assertFalse(pub2_done.is_set())

            writer2_release.set()
            t_writer2.join(timeout=10.0)
            self.assertFalse(t_writer2.is_alive())
            self.assertEqual(writer2_err, [])
            self.assertTrue(writer2_done.is_set())

            t_pub2.join(timeout=10.0)
            self.assertFalse(t_pub2.is_alive())
            self.assertEqual(pub2_err, [])
            self.assertTrue(pub2_done.is_set())
        finally:
            writer2_release.set()
            t_writer2.join(timeout=10.0)
            t_pub2.join(timeout=10.0)

        with self.connect() as verify_conn:
            st2, count2_db, mh2_db = verify_conn.execute(
                "SELECT status, item_count, manifest_hash FROM vector_collections WHERE id = %s",
                (col2,),
            ).fetchone()
            self.assertEqual(st2, "ready")
            self.assertEqual(count2_db, 1)
            self.assertEqual(mh2_db, mh2)

            # Ready mapping contains both regions committed before ready
            regions2 = verify_conn.execute(
                "SELECT item_id, ordinal, region FROM vector_item_regions WHERE collection_id = %s ORDER BY ordinal",
                (col2,),
            ).fetchall()
            self.assertEqual(len(regions2), 2)
            self.assertEqual(regions2[0], ("item_2", 0, reg0_2))
            self.assertEqual(regions2[1], ("item_2", 1, reg1_2))

            cnt2_rec, mh2_rec = V.manifest(verify_conn, col2)
            self.assertEqual(cnt2_rec, 1)
            self.assertEqual(mh2_rec, mh2)

            # Further writes to ready collection are rejected
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                verify_conn.execute(
                    "INSERT INTO vector_item_regions VALUES (%s, %s, %s, %s)",
                    (col2, "item_2", 2, Jsonb({"page": 3})),
                )


if __name__ == "__main__":
    unittest.main()
