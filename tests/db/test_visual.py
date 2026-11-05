"""Real PostgreSQL integration tests for 0005_parse_visual.sql (SPEC 19.2, 19.6)."""
import uuid

from psycopg import errors
from psycopg.types.json import Jsonb

from tests.db.base import FreshDatabase, h


class TestParseVisualSchema(FreshDatabase):
    """Verify schema constraints, triggers, composite FKs, and append-only guards
    for parse_runs, visual_objects, visual_object_regions, visual_observations,
    evidence_relations, evidence_verifications, derivations, and derivation_inputs.
    """

    def _make_resource(self, conn, project_id, actor_id, resource_type="document", logical_id=None, revision=1):
        rid = uuid.uuid4()
        logical_id = logical_id or f"{resource_type}_{rid.hex[:8]}"
        conn.execute(
            """
            INSERT INTO resource_versions
                (id, project_id, resource_type, logical_id, revision, content_hash, c14n_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'v1', %s)
            """,
            (rid, project_id, resource_type, logical_id, revision, h(f"{logical_id}:{revision}"), actor_id),
        )
        return rid

    def _make_operation(self, conn, project_id, actor_id, kind="parse.run", status="succeeded"):
        op_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO operations (id, project_id, kind, actor_id, trace_id, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (op_id, project_id, kind, actor_id, h(str(op_id))[:32], status),
        )
        return op_id

    def _make_artifact(self, conn, project_id, state="committed", mime="application/json"):
        art_id = uuid.uuid4()
        key = f"proj/{project_id}/art/{art_id}"
        if state == "staged":
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, project_id, object_key, object_version, sha256, size_bytes, mime, state, retention)
                VALUES (%s, %s, %s, 'v1', %s, 1024, %s, 'staged', 'standard')
                """,
                (art_id, project_id, key, h(key), mime),
            )
        elif state == "committed":
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, project_id, object_key, object_version, sha256, size_bytes, mime, state, retention, committed_at)
                VALUES (%s, %s, %s, 'v1', %s, 1024, %s, 'committed', 'standard', now())
                """,
                (art_id, project_id, key, h(key), mime),
            )
        else:
            raise ValueError(f"Unsupported helper state: {state}")
        return art_id

    def _make_document(self, conn, project_id, actor_id, page_count=2):
        res_id = self._make_resource(conn, project_id, actor_id, "document")
        pdf_art = self._make_artifact(conn, project_id, state="committed", mime="application/pdf")
        doc_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (doc_id, project_id, res_id, pdf_art, h(str(doc_id)), page_count),
        )
        for idx in range(page_count):
            conn.execute(
                """
                INSERT INTO pages (document_version_id, page_index, page_label, rotation, width_pt, height_pt)
                VALUES (%s, %s, %s, 0, 612, 792)
                """,
                (doc_id, idx, str(idx + 1)),
            )
        return doc_id

    def _make_evidence_unit(self, conn, project_id, doc_id, key=None):
        eu_id = uuid.uuid4()
        key = key or f"ev_{eu_id.hex[:8]}"
        conn.execute(
            """
            INSERT INTO evidence_units
                (id, project_id, document_version_id, evidence_key, raw_content, normalized_content, extractor_revision, ocr_confidence)
            VALUES (%s, %s, %s, %s, 'raw text', 'norm text', 'unstructured-1.0', NULL)
            """,
            (eu_id, project_id, doc_id, key),
        )
        return eu_id

    def _make_parse_run(
        self,
        conn,
        project_id,
        doc_id,
        op_id,
        raw_art_id=None,
        manifest_art_id=None,
        requested_profile="hi_res",
        effective_profile="hi_res",
        fallback_reason=None,
        status="succeeded",
        cost_usd="0.15",
    ):
        raw_art_id = raw_art_id or self._make_artifact(conn, project_id, state="committed")
        manifest_art_id = manifest_art_id or self._make_artifact(conn, project_id, state="committed")
        pr_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO parse_runs (
                id, project_id, document_version_id, parser_name, package_version,
                output_schema, requested_profile, effective_profile, fallback_reason,
                raw_artifact_id, manifest_artifact_id, cost_usd, status, operation_id
            )
            VALUES (%s, %s, %s, 'unstructured', 'unknown', 'lab_parse_v1', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pr_id,
                project_id,
                doc_id,
                requested_profile,
                effective_profile,
                fallback_reason,
                raw_art_id,
                manifest_art_id,
                cost_usd,
                status,
                op_id,
            ),
        )
        return pr_id

    def _setup_fixture(self, conn):
        p1, a1 = self.make_project(conn)
        with conn.transaction():
            doc1 = self._make_document(conn, p1, a1, page_count=2)
            op1 = self._make_operation(conn, p1, a1)
            eu1 = self._make_evidence_unit(conn, p1, doc1, key="ev_1")
            eu2 = self._make_evidence_unit(conn, p1, doc1, key="ev_2")
            pr1 = self._make_parse_run(conn, p1, doc1, op1)
        return p1, a1, doc1, op1, eu1, eu2, pr1

    def test_parse_runs_fallback_reason_and_cost_checks(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, _, _, _ = self._setup_fixture(conn)

            # Valid: requested == effective and fallback_reason is NULL, cost_usd is NULL (unknown cost)
            with conn.transaction():
                self._make_parse_run(
                    conn,
                    p1,
                    doc1,
                    op1,
                    requested_profile="hi_res",
                    effective_profile="hi_res",
                    fallback_reason=None,
                    cost_usd=None,
                )

            # Valid: requested != effective and fallback_reason is provided
            with conn.transaction():
                self._make_parse_run(
                    conn,
                    p1,
                    doc1,
                    op1,
                    requested_profile="hi_res",
                    effective_profile="fast",
                    fallback_reason="ocr_model_unavailable",
                    status="needs_review",
                )

            # Invalid: requested != effective but fallback_reason is NULL
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    self._make_parse_run(
                        conn,
                        p1,
                        doc1,
                        op1,
                        requested_profile="hi_res",
                        effective_profile="fast",
                        fallback_reason=None,
                    )

            # Invalid: requested == effective but fallback_reason is NOT NULL
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    self._make_parse_run(
                        conn,
                        p1,
                        doc1,
                        op1,
                        requested_profile="hi_res",
                        effective_profile="hi_res",
                        fallback_reason="unexpected_reason",
                    )

            # Invalid: negative cost_usd
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    self._make_parse_run(
                        conn,
                        p1,
                        doc1,
                        op1,
                        cost_usd="-0.01",
                    )

    def test_visual_objects_verified_requires_committed_crop_and_regions(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, _, pr1 = self._setup_fixture(conn)
            with conn.transaction():
                staged_crop = self._make_artifact(conn, p1, state="staged", mime="image/png")
                committed_crop = self._make_artifact(conn, p1, state="committed", mime="image/png")
                context_art = self._make_artifact(conn, p1, state="committed", mime="image/png")

            # Invalid: status='verified' with crop_artifact_id=NULL
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_objects
                            (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                        VALUES (%s, %s, %s, %s, 'chart', NULL, %s, 'verified')
                        """,
                        (uuid.uuid4(), p1, pr1, eu1, Jsonb({"dpi": 150})),
                    )

            # Invalid: status='verified' with staged (uncommitted) crop artifact on INSERT
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_objects
                            (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                        VALUES (%s, %s, %s, %s, 'chart', %s, %s, 'verified')
                        """,
                        (uuid.uuid4(), p1, pr1, eu1, staged_crop, Jsonb({"dpi": 150})),
                    )

            # Valid as 'extracted' with staged crop, but UPDATE to 'verified' must fail while crop is staged
            vo_staged_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO visual_objects
                        (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                    VALUES (%s, %s, %s, %s, 'chart', %s, %s, 'extracted')
                    """,
                    (vo_staged_id, p1, pr1, eu1, staged_crop, Jsonb({"dpi": 150})),
                )

            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE visual_objects SET status = 'verified' WHERE id = %s",
                        (vo_staged_id,),
                    )

            # Valid: status='verified' with committed crop artifact + child visual object + regions
            vo_verified_id = uuid.uuid4()
            vo_child_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO visual_objects (
                        id, project_id, parse_run_id, evidence_unit_id, object_type,
                        parent_id, crop_artifact_id, context_artifact_id, transform, status
                    )
                    VALUES (%s, %s, %s, %s, 'chart', NULL, %s, %s, %s, 'verified')
                    """,
                    (vo_verified_id, p1, pr1, eu1, committed_crop, context_art, Jsonb({"dpi": 300})),
                )
                conn.execute(
                    """
                    INSERT INTO visual_objects (
                        id, project_id, parse_run_id, evidence_unit_id, object_type,
                        parent_id, crop_artifact_id, transform, status
                    )
                    VALUES (%s, %s, %s, NULL, 'diagram', %s, NULL, %s, 'extracted')
                    """,
                    (vo_child_id, p1, pr1, vo_verified_id, Jsonb({"sub": 1})),
                )
                conn.execute(
                    """
                    INSERT INTO visual_object_regions
                        (visual_object_id, ordinal, document_version_id, page_index, x0, y0, x1, y1)
                    VALUES (%s, 0, %s, 0, 0.1, 0.2, 0.8, 0.7)
                    """,
                    (vo_verified_id, doc1),
                )

            # Invalid: tombstoning a crop artifact while referenced by a verified visual_object
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        UPDATE artifacts
                        SET state = 'tombstoned', tombstoned_at = now(), tombstone_reason = 'test'
                        WHERE id = %s
                        """,
                        (committed_crop,),
                    )

            # Invalid region coordinates (x0 >= x1)
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_object_regions
                            (visual_object_id, ordinal, document_version_id, page_index, x0, y0, x1, y1)
                        VALUES (%s, 1, %s, 0, 0.8, 0.2, 0.3, 0.7)
                        """,
                        (vo_verified_id, doc1),
                    )

            # Invalid region page reference
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_object_regions
                            (visual_object_id, ordinal, document_version_id, page_index, x0, y0, x1, y1)
                        VALUES (%s, 2, %s, 99, 0.1, 0.2, 0.8, 0.7)
                        """,
                        (vo_verified_id, doc1),
                    )

    def test_visual_observations_estimates_and_append_only(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, _, pr1 = self._setup_fixture(conn)
            with conn.transaction():
                crop_art = self._make_artifact(conn, p1, state="committed", mime="image/png")
                data_art = self._make_artifact(conn, p1, state="committed", mime="application/json")
                vo_id = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO visual_objects
                        (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                    VALUES (%s, %s, %s, %s, 'chart', %s, %s, 'verified')
                    """,
                    (vo_id, p1, pr1, eu1, crop_art, Jsonb({})),
                )

            # Invalid: estimated_from_plot without estimate_low / estimate_high
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_observations (
                            id, project_id, visual_object_id, layer, observation_type,
                            data_artifact_id, value_source, estimate_low, estimate_high,
                            confidence, verification, producer_operation_id
                        )
                        VALUES (%s, %s, %s, 'L2', 'chart_series', %s, 'estimated_from_plot', NULL, NULL, NULL, 'unverified', %s)
                        """,
                        (uuid.uuid4(), p1, vo_id, data_art, op1),
                    )

            # Invalid: estimated_from_plot with estimate_low > estimate_high
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_observations (
                            id, project_id, visual_object_id, layer, observation_type,
                            data_artifact_id, value_source, estimate_low, estimate_high,
                            confidence, verification, producer_operation_id
                        )
                        VALUES (%s, %s, %s, 'L2', 'chart_series', %s, 'estimated_from_plot', 25.0, 20.0, NULL, 'unverified', %s)
                        """,
                        (uuid.uuid4(), p1, vo_id, data_art, op1),
                    )

            # Invalid: observed_label with non-null estimate bounds
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_observations (
                            id, project_id, visual_object_id, layer, observation_type,
                            data_artifact_id, value_source, estimate_low, estimate_high,
                            confidence, verification, producer_operation_id
                        )
                        VALUES (%s, %s, %s, 'L2', 'table_cell', %s, 'observed_label', 10.0, 12.0, 0.9, 'unverified', %s)
                        """,
                        (uuid.uuid4(), p1, vo_id, data_art, op1),
                    )

            # Invalid: layer='L3' in visual_observations
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_observations (
                            id, project_id, visual_object_id, layer, observation_type,
                            data_artifact_id, value_source, confidence, verification, producer_operation_id
                        )
                        VALUES (%s, %s, %s, 'L3', 'summary', %s, NULL, NULL, 'unverified', %s)
                        """,
                        (uuid.uuid4(), p1, vo_id, data_art, op1),
                    )

            # Valid: estimated_from_plot with estimate_low <= estimate_high and NULL confidence
            obs_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO visual_observations (
                        id, project_id, visual_object_id, layer, observation_type,
                        data_artifact_id, value_source, estimate_low, estimate_high,
                        confidence, verification, producer_operation_id
                    )
                    VALUES (%s, %s, %s, 'L2', 'chart_series', %s, 'estimated_from_plot', 18.5, 21.0, NULL, 'verified', %s)
                    """,
                    (obs_id, p1, vo_id, data_art, op1),
                )

            # Append-only guard: UPDATE and DELETE must raise IntegrityConstraintViolation
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE visual_observations SET verification = 'disputed' WHERE id = %s",
                        (obs_id,),
                    )

            with self.assertRaises(errors.IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM visual_observations WHERE id = %s",
                        (obs_id,),
                    )

    def test_evidence_relations_self_ref_and_same_document_checks(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, eu2, _ = self._setup_fixture(conn)
            with conn.transaction():
                rel_res_id = self._make_resource(conn, p1, a1, "evidence_relation")
                doc2 = self._make_document(conn, p1, a1, page_count=1)
                eu_other_doc = self._make_evidence_unit(conn, p1, doc2, key="ev_doc2")

            # Invalid: self-reference (from_evidence_id == to_evidence_id)
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_relations
                            (id, project_id, from_evidence_id, to_evidence_id, relation_type, resource_id, validation)
                        VALUES (%s, %s, %s, %s, 'continues', %s, 'unvalidated')
                        """,
                        (uuid.uuid4(), p1, eu1, eu1, rel_res_id),
                    )

            # Invalid: endpoints from different documents in the same project
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_relations
                            (id, project_id, from_evidence_id, to_evidence_id, relation_type, resource_id, validation)
                        VALUES (%s, %s, %s, %s, 'refers_to', %s, 'unvalidated')
                        """,
                        (uuid.uuid4(), p1, eu1, eu_other_doc, rel_res_id),
                    )

            # Valid: distinct evidence_units in the same document
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO evidence_relations
                        (id, project_id, from_evidence_id, to_evidence_id, relation_type, resource_id, validation)
                    VALUES (%s, %s, %s, %s, 'table_continuation', %s, 'validated')
                    """,
                    (uuid.uuid4(), p1, eu1, eu2, rel_res_id),
                )

    def test_evidence_verifications_target_xor_and_append_only(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, _, pr1 = self._setup_fixture(conn)
            with conn.transaction():
                img_art = self._make_artifact(conn, p1, state="committed", mime="image/png")
                vo_id = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO visual_objects
                        (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                    VALUES (%s, %s, %s, %s, 'table', %s, %s, 'extracted')
                    """,
                    (vo_id, p1, pr1, eu1, img_art, Jsonb({})),
                )

            # Invalid: neither target specified
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_verifications (
                            id, project_id, evidence_unit_id, visual_object_id,
                            oracle, result, reason, actor_id, source_image_artifact_id
                        )
                        VALUES (%s, %s, NULL, NULL, 'human', 'pass', 'checked', %s, %s)
                        """,
                        (uuid.uuid4(), p1, a1, img_art),
                    )

            # Invalid: both targets specified
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_verifications (
                            id, project_id, evidence_unit_id, visual_object_id,
                            oracle, result, reason, actor_id, source_image_artifact_id
                        )
                        VALUES (%s, %s, %s, %s, 'human', 'pass', 'checked', %s, %s)
                        """,
                        (uuid.uuid4(), p1, eu1, vo_id, a1, img_art),
                    )

            # Invalid: empty/blank reason
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_verifications (
                            id, project_id, evidence_unit_id, visual_object_id,
                            oracle, result, reason, actor_id, source_image_artifact_id
                        )
                        VALUES (%s, %s, %s, NULL, 'human', 'pass', '   ', %s, %s)
                        """,
                        (uuid.uuid4(), p1, eu1, a1, img_art),
                    )

            # Valid: exactly one target (visual_object_id)
            ver_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO evidence_verifications (
                        id, project_id, evidence_unit_id, visual_object_id,
                        oracle, result, reason, actor_id, source_image_artifact_id
                    )
                    VALUES (%s, %s, NULL, %s, 'vlm', 'pass', 'Confirmed table headers against page crop', %s, %s)
                    """,
                    (ver_id, p1, vo_id, a1, img_art),
                )

            # Append-only guard: UPDATE and DELETE must raise IntegrityConstraintViolation
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE evidence_verifications SET result = 'fail' WHERE id = %s",
                        (ver_id,),
                    )

            with self.assertRaises(errors.IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM evidence_verifications WHERE id = %s",
                        (ver_id,),
                    )

    def test_derivations_require_inputs_before_verified(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, _, pr1 = self._setup_fixture(conn)
            with conn.transaction():
                crop_art = self._make_artifact(conn, p1, state="committed", mime="image/png")
                data_art = self._make_artifact(conn, p1, state="committed", mime="application/json")
                vo_id = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO visual_objects
                        (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, transform, status)
                    VALUES (%s, %s, %s, %s, 'chart', %s, %s, 'verified')
                    """,
                    (vo_id, p1, pr1, eu1, crop_art, Jsonb({})),
                )
                obs_id = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO visual_observations (
                        id, project_id, visual_object_id, layer, observation_type,
                        data_artifact_id, value_source, confidence, verification, producer_operation_id
                    )
                    VALUES (%s, %s, %s, 'L2', 'chart_axis', %s, 'observed_label', 0.95, 'verified', %s)
                    """,
                    (obs_id, p1, vo_id, data_art, op1),
                )

            # Invalid: inserting a derivation directly as 'verified' without inputs
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO derivations
                            (id, project_id, layer, kind, formula, model, prompt_revision, output_claims, operation_id, status)
                        VALUES (%s, %s, 'L3', 'calculation', %s, 'gemini-pro', 'p_v1', %s, %s, 'verified')
                        """,
                        (uuid.uuid4(), p1, Jsonb({"op": "diff"}), Jsonb([{"claim": "delta is 5"}]), op1),
                    )

            # Create an 'unverified' derivation
            deriv_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO derivations
                        (id, project_id, layer, kind, formula, model, prompt_revision, output_claims, operation_id, status)
                    VALUES (%s, %s, 'L3', 'calculation', %s, 'gemini-pro', 'p_v1', %s, %s, 'unverified')
                    """,
                    (deriv_id, p1, Jsonb({"op": "diff"}), Jsonb([{"claim": "delta is 5"}]), op1),
                )

            # Invalid: updating derivation to 'verified' when it still has 0 derivation_inputs
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE derivations SET status = 'verified' WHERE id = %s",
                        (deriv_id,),
                    )

            # Invalid derivation_input with both evidence_unit_id and visual_observation_id set
            with self.assertRaises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO derivation_inputs
                            (project_id, derivation_id, evidence_unit_id, visual_observation_id)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (p1, deriv_id, eu1, obs_id),
                    )

            # Attach valid inputs and verify the derivation
            input_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO derivation_inputs
                        (id, project_id, derivation_id, evidence_unit_id, visual_observation_id)
                    VALUES (%s, %s, %s, NULL, %s)
                    """,
                    (input_id, p1, deriv_id, obs_id),
                )
                conn.execute(
                    "UPDATE derivations SET status = 'verified' WHERE id = %s",
                    (deriv_id,),
                )

            # Invalid: deleting the sole input of a verified derivation. Since
            # 0007 derivation inputs are append-only, so the stronger guard fires.
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM derivation_inputs WHERE id = %s",
                        (input_id,),
                    )

    def test_cross_project_references_rejected(self):
        with self.connect() as conn:
            p1, a1, doc1, op1, eu1, eu2, pr1 = self._setup_fixture(conn)
            p2, a2 = self.make_project(conn)
            with conn.transaction():
                doc2 = self._make_document(conn, p2, a2, page_count=1)
                op2 = self._make_operation(conn, p2, a2)
                art2 = self._make_artifact(conn, p2, state="committed", mime="image/png")
                eu_p2 = self._make_evidence_unit(conn, p2, doc2, key="ev_p2")
                res_p2 = self._make_resource(conn, p2, a2, "evidence_relation")
                deriv_p1 = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO derivations
                        (id, project_id, layer, kind, output_claims, operation_id, status)
                    VALUES (%s, %s, 'L3', 'summary', %s, %s, 'unverified')
                    """,
                    (deriv_p1, p1, Jsonb([]), op1),
                )

            # parse_runs referencing document_version from p2 in project p1
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    self._make_parse_run(conn, p1, doc2, op1)

            # visual_objects referencing crop_artifact from p2 in project p1
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_objects
                            (id, project_id, parse_run_id, object_type, crop_artifact_id, transform, status)
                        VALUES (%s, %s, %s, 'image', %s, %s, 'extracted')
                        """,
                        (uuid.uuid4(), p1, pr1, art2, Jsonb({})),
                    )

            # evidence_relations referencing evidence_unit from p2 in project p1
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_relations
                            (id, project_id, from_evidence_id, to_evidence_id, relation_type, resource_id, validation)
                        VALUES (%s, %s, %s, %s, 'continues', %s, 'unvalidated')
                        """,
                        (uuid.uuid4(), p1, eu1, eu_p2, res_p2),
                    )

            # evidence_verifications referencing evidence_unit from p2 in project p1
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_verifications
                            (id, project_id, evidence_unit_id, oracle, result, reason, actor_id)
                        VALUES (%s, %s, %s, 'ocr', 'pass', 'cross project test', %s)
                        """,
                        (uuid.uuid4(), p1, eu_p2, a1),
                    )

            # derivations referencing operation from p2 in project p1
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO derivations
                            (id, project_id, layer, kind, output_claims, operation_id, status)
                        VALUES (%s, %s, 'L3', 'summary', %s, %s, 'unverified')
                        """,
                        (uuid.uuid4(), p1, Jsonb([]), op2),
                    )

            # derivation_inputs referencing evidence_unit from p2 for a p1 derivation
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO derivation_inputs
                            (project_id, derivation_id, evidence_unit_id)
                        VALUES (%s, %s, %s)
                        """,
                        (p1, deriv_p1, eu_p2),
                    )
