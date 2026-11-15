"""Real PostgreSQL integration tests for document ingestion (SPEC R01, 10.1-10.2, #7)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
import uuid

import psycopg
from psycopg.rows import dict_row

from lab_db.ingest import (
    IngestRejected,
    Inspection,
    PageGeometry,
    ingest_document,
)
from lab_db.storage import LocalArtifactStore

try:
    from .base import FreshDatabase
except ImportError:
    try:
        from tests.db.base import FreshDatabase
    except ImportError:
        from base import FreshDatabase  # type: ignore[no-redef]


def synthetic_pdf(pages: int) -> bytes:
    """A small valid PDF with ``pages`` distinct text pages, built by hand
    (no third-party code). ingest_document treats the bytes as opaque; the
    page inspection is passed in as data, as the parser service does."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    font = 3 + 2 * pages
    for i in range(pages):
        page_no, content_no = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_no} 0 R")
        stream = f"BT /F1 24 Tf 72 700 Td (synthetic page {i + 1}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content_no} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>".encode()
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


class IngestDocumentTests(FreshDatabase):
    """Verify atomic document ingestion, rejection audit, flagged imports, consistency checks, and rollback."""

    pdf_bytes: bytes
    pdf_sha256: str
    pdf_size: int

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.pdf_bytes = synthetic_pdf(21)
        cls.pdf_sha256 = hashlib.sha256(cls.pdf_bytes).hexdigest()
        cls.pdf_size = len(cls.pdf_bytes)

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.store = LocalArtifactStore(self.root)

    @staticmethod
    def _make_pages(count: int = 21) -> list[PageGeometry]:
        rotations = (0, 90, 180, 270)
        return [
            PageGeometry(
                page_index=i,
                page_label=f"p-{i + 1}",
                rotation=rotations[i % len(rotations)],
                display_width_pt=612.0,
                display_height_pt=792.0,
            )
            for i in range(count)
        ]

    @staticmethod
    def _decode_jsonb(val: Any) -> Any:
        if isinstance(val, str):
            return json.loads(val)
        return val

    def test_accept_f01_pages_21_creates_document_pages_artifact_audit_and_outbox(self) -> None:
        """Accepting a 21-page synthetic PDF persists artifact, resource_version, document_version, 21 pages, audit, and outbox."""
        inspection = Inspection(
            outcome="accept",
            reason=None,
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=21,
            warnings=(),
        )
        pages = self._make_pages(21)

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            doc_id = ingest_document(
                conn,
                self.store,
                project_id=project_id,
                actor_id=actor_id,
                logical_id="f01-pages-21",
                data=self.pdf_bytes,
                inspection=inspection,
                pages=pages,
            )
            self.assertIsInstance(doc_id, uuid.UUID)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM document_versions WHERE project_id = %s",
                    (project_id,),
                )
                doc_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM pages WHERE document_version_id = %s ORDER BY page_index ASC",
                    (doc_id,),
                )
                page_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s",
                    (project_id,),
                )
                artifact_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM resource_versions WHERE project_id = %s",
                    (project_id,),
                )
                rv_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s",
                    (project_id,),
                )
                audit_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM outbox_events WHERE project_id = %s",
                    (project_id,),
                )
                outbox_rows = cur.fetchall()

        self.assertEqual(len(doc_rows), 1)
        doc_row = doc_rows[0]
        self.assertEqual(doc_row["id"], doc_id)
        self.assertEqual(doc_row["page_count"], 21)
        self.assertEqual(doc_row["sha256"], self.pdf_sha256)

        self.assertEqual(len(page_rows), 21)
        for idx, page_row in enumerate(page_rows):
            self.assertEqual(page_row["page_index"], idx)
            self.assertEqual(page_row["page_label"], f"p-{idx + 1}")
            self.assertEqual(page_row["rotation"], (0, 90, 180, 270)[idx % 4])
            self.assertEqual(float(page_row["width_pt"]), 612.0)
            self.assertEqual(float(page_row["height_pt"]), 792.0)

        self.assertEqual(len(artifact_rows), 1)
        art_row = artifact_rows[0]
        self.assertEqual(art_row["id"], doc_row["artifact_id"])
        self.assertEqual(art_row.get("state", art_row.get("status")), "committed")
        self.assertEqual(art_row["sha256"], self.pdf_sha256)
        self.assertEqual(art_row["size_bytes"], self.pdf_size)
        self.assertEqual(art_row["mime"], "application/pdf")
        self.assertEqual(art_row["retention"], "restricted")
        self.assertEqual(self.store.open(art_row["object_key"]), self.pdf_bytes)

        self.assertEqual(len(rv_rows), 1)
        rv_row = rv_rows[0]
        self.assertEqual(rv_row["id"], doc_row["resource_id"])
        self.assertEqual(rv_row["resource_type"], "document_version")
        self.assertEqual(rv_row["logical_id"], "f01-pages-21")
        self.assertEqual(rv_row["revision"], 1)
        self.assertEqual(rv_row["content_hash"], self.pdf_sha256)
        self.assertEqual(rv_row["c14n_version"], "raw-bytes")

        self.assertEqual(len(audit_rows), 1)
        audit_row = audit_rows[0]
        self.assertEqual(audit_row["action"], "document.imported")
        self.assertEqual(audit_row["outcome"], "succeeded")
        self.assertEqual(audit_row["resource_type"], "document_version")
        self.assertEqual(audit_row["resource_id"], rv_row["id"])
        self.assertIsNone(audit_row["reason"])

        self.assertEqual(len(outbox_rows), 1)
        outbox_row = outbox_rows[0]
        self.assertEqual(outbox_row["topic"], "document.imported")
        payload = self._decode_jsonb(outbox_row["payload"])
        self.assertEqual(payload["page_count"], 21)
        self.assertIs(payload["flagged"], False)
        self.assertEqual(payload["warnings"], [])

    def test_reimport_same_logical_id_creates_revision_2(self) -> None:
        """Importing the same logical_id again creates revision 2 without overwriting revision 1."""
        inspection = Inspection(
            outcome="accept",
            reason=None,
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=21,
            warnings=(),
        )
        pages = self._make_pages(21)

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            doc_id_1 = ingest_document(
                conn,
                self.store,
                project_id=project_id,
                actor_id=actor_id,
                logical_id="doc-same-logical",
                data=self.pdf_bytes,
                inspection=inspection,
                pages=pages,
            )
            doc_id_2 = ingest_document(
                conn,
                self.store,
                project_id=project_id,
                actor_id=actor_id,
                logical_id="doc-same-logical",
                data=self.pdf_bytes,
                inspection=inspection,
                pages=pages,
            )
            self.assertNotEqual(doc_id_1, doc_id_2)
            # Same bytes in the same project reuse one committed artifact row.
            arts = conn.execute("SELECT artifact_id FROM document_versions WHERE id = ANY(%s)",
                                ([doc_id_1, doc_id_2],)).fetchall()
            self.assertEqual(len({a[0] for a in arts}), 1)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, revision, content_hash FROM resource_versions "
                    "WHERE project_id = %s AND resource_type = 'document_version' AND logical_id = %s "
                    "ORDER BY revision ASC",
                    (project_id, "doc-same-logical"),
                )
                rv_rows = cur.fetchall()

                cur.execute(
                    "SELECT id, resource_id FROM document_versions WHERE project_id = %s",
                    (project_id,),
                )
                doc_rows = cur.fetchall()

        self.assertEqual([r["revision"] for r in rv_rows], [1, 2])
        self.assertEqual(len(doc_rows), 2)
        doc_by_id = {r["id"]: r["resource_id"] for r in doc_rows}
        self.assertEqual(doc_by_id[doc_id_1], rv_rows[0]["id"])
        self.assertEqual(doc_by_id[doc_id_2], rv_rows[1]["id"])

    def test_reject_records_failed_audit_and_creates_no_document_or_artifact(self) -> None:
        """Rejected inspection raises IngestRejected, writes failed operation + audit, and leaves 0 document/artifact rows."""
        inspection = Inspection(
            outcome="reject",
            reason="encrypted_pdf",
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=None,
            warnings=(),
        )

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            with self.assertRaises(IngestRejected) as cm:
                ingest_document(
                    conn,
                    self.store,
                    project_id=project_id,
                    actor_id=actor_id,
                    logical_id="doc-rejected",
                    data=self.pdf_bytes,
                    inspection=inspection,
                    pages=[],
                )
            self.assertEqual(cm.exception.reason, "encrypted_pdf")

            with conn.cursor(row_factory=dict_row) as cur:
                for table in ("artifacts", "resource_versions", "document_versions", "outbox_events"):
                    cur.execute(
                        f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s",
                        (project_id,),
                    )
                    self.assertEqual(cur.fetchone()["cnt"], 0, f"expected 0 rows in {table}")

                cur.execute("SELECT count(*) AS cnt FROM pages p JOIN document_versions dv ON dv.id = p.document_version_id WHERE dv.project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

                cur.execute(
                    "SELECT * FROM operations WHERE project_id = %s",
                    (project_id,),
                )
                op_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s",
                    (project_id,),
                )
                audit_rows = cur.fetchall()

        self.assertEqual(len(op_rows), 1)
        self.assertEqual(op_rows[0]["kind"], "document.import")
        self.assertEqual(op_rows[0]["status"], "failed")

        self.assertEqual(len(audit_rows), 1)
        audit_row = audit_rows[0]
        self.assertEqual(audit_row["action"], "document.rejected")
        self.assertEqual(audit_row["outcome"], "failed")
        self.assertEqual(audit_row["reason"], "encrypted_pdf")
        self.assertEqual(audit_row["resource_type"], "document_version")
        self.assertIsNone(audit_row["resource_id"])
        self.assertEqual(audit_row["operation_id"], op_rows[0]["id"])

    def test_flag_persists_warnings_and_flagged_audit_reason(self) -> None:
        """Flagged inspection succeeds, sets outbox flagged=True with warnings, and prefixes audit reason with 'flagged:'."""
        inspection = Inspection(
            outcome="flag",
            reason="mixed_scan_low_dpi",
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=21,
            warnings=("scan_page_4", "low_dpi_120"),
        )
        pages = self._make_pages(21)

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            doc_id = ingest_document(
                conn,
                self.store,
                project_id=project_id,
                actor_id=actor_id,
                logical_id="doc-flagged",
                data=self.pdf_bytes,
                inspection=inspection,
                pages=pages,
            )
            self.assertIsInstance(doc_id, uuid.UUID)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM audit_events WHERE project_id = %s",
                    (project_id,),
                )
                audit_rows = cur.fetchall()

                cur.execute(
                    "SELECT * FROM outbox_events WHERE project_id = %s",
                    (project_id,),
                )
                outbox_rows = cur.fetchall()

        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["action"], "document.imported")
        self.assertEqual(audit_rows[0]["outcome"], "succeeded")
        self.assertIsNotNone(audit_rows[0]["reason"])
        self.assertTrue(audit_rows[0]["reason"].startswith("flagged:"))
        self.assertEqual(audit_rows[0]["reason"], "flagged: mixed_scan_low_dpi")

        self.assertEqual(len(outbox_rows), 1)
        payload = self._decode_jsonb(outbox_rows[0]["payload"])
        self.assertIs(payload["flagged"], True)
        self.assertEqual(payload["warnings"], ["scan_page_4", "low_dpi_120"])
        self.assertEqual(payload["page_count"], 21)

    def test_consistency_errors_raise_value_error_without_writing_anything(self) -> None:
        """Mismatched sha256, size_bytes, page_count, or non-contiguous page_index raise ValueError before any writes."""
        valid_pages = self._make_pages(21)
        valid_inspection = Inspection(
            outcome="accept",
            reason=None,
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=21,
            warnings=(),
        )

        gapped_pages = list(valid_pages)
        gapped_pages[-1] = PageGeometry(
            page_index=21,
            page_label="p-22",
            rotation=0,
            display_width_pt=612.0,
            display_height_pt=792.0,
        )

        bad_rotation_pages = list(valid_pages)
        bad_rotation_pages[0] = PageGeometry(
            page_index=0,
            page_label="p-1",
            rotation=45,
            display_width_pt=612.0,
            display_height_pt=792.0,
        )

        bad_dim_pages = list(valid_pages)
        bad_dim_pages[0] = PageGeometry(
            page_index=0,
            page_label="p-1",
            rotation=0,
            display_width_pt=0.0,
            display_height_pt=792.0,
        )

        cases: list[tuple[str, Inspection, list[PageGeometry]]] = [
            (
                "sha256_mismatch",
                Inspection(
                    outcome="accept",
                    reason=None,
                    sha256="0" * 64,
                    size_bytes=self.pdf_size,
                    page_count=21,
                    warnings=(),
                ),
                valid_pages,
            ),
            (
                "size_bytes_mismatch",
                Inspection(
                    outcome="accept",
                    reason=None,
                    sha256=self.pdf_sha256,
                    size_bytes=self.pdf_size + 5,
                    page_count=21,
                    warnings=(),
                ),
                valid_pages,
            ),
            (
                "page_count_mismatch",
                Inspection(
                    outcome="accept",
                    reason=None,
                    sha256=self.pdf_sha256,
                    size_bytes=self.pdf_size,
                    page_count=20,
                    warnings=(),
                ),
                valid_pages,
            ),
            (
                "page_count_none",
                Inspection(
                    outcome="accept",
                    reason=None,
                    sha256=self.pdf_sha256,
                    size_bytes=self.pdf_size,
                    page_count=None,
                    warnings=(),
                ),
                valid_pages,
            ),
            (
                "non_contiguous_page_index",
                valid_inspection,
                gapped_pages,
            ),
            (
                "invalid_rotation",
                valid_inspection,
                bad_rotation_pages,
            ),
            (
                "non_positive_dimensions",
                valid_inspection,
                bad_dim_pages,
            ),
        ]

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            for name, bad_insp, bad_pages in cases:
                with self.subTest(case=name):
                    with self.assertRaises(ValueError):
                        ingest_document(
                            conn,
                            self.store,
                            project_id=project_id,
                            actor_id=actor_id,
                            logical_id=f"bad-{name}",
                            data=self.pdf_bytes,
                            inspection=bad_insp,
                            pages=bad_pages,
                        )

            with conn.cursor(row_factory=dict_row) as cur:
                for table in (
                    "artifacts",
                    "operations",
                    "resource_versions",
                    "document_versions",
                    "audit_events",
                    "outbox_events",
                ):
                    cur.execute(
                        f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s",
                        (project_id,),
                    )
                    self.assertEqual(cur.fetchone()["cnt"], 0, f"expected 0 rows in {table}")

                cur.execute("SELECT count(*) AS cnt FROM pages p JOIN document_versions dv ON dv.id = p.document_version_id WHERE dv.project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

    def test_pages_write_failure_rolls_back_resource_and_document_version(self) -> None:
        """Failure while inserting pages rolls back resource_versions, document_versions, pages, audit, and outbox.

        Note on artifact lifecycle: ``store_and_register`` commits the content-addressed
        artifact in its own transaction before the metadata transaction begins. When the
        subsequent transaction fails during ``pages`` insertion (here via the database
        trigger rejecting ``page_index >= page_count``), all metadata tables roll back
        cleanly while the ``artifacts`` row remains ``committed``. That unreferenced
        committed artifact is acceptable and is handled by orphan artifact policies
        separate from staged GC.
        """
        inspection = Inspection(
            outcome="accept",
            reason=None,
            sha256=self.pdf_sha256,
            size_bytes=self.pdf_size,
            page_count=21,
            warnings=(),
        )
        pages = self._make_pages(21)

        def insert_out_of_bounds_page(
            conn: psycopg.Connection,
            doc_version_id: uuid.UUID,
            _pages: list[PageGeometry],
        ) -> None:
            conn.execute(
                "INSERT INTO pages "
                "(document_version_id, page_index, page_label, rotation, width_pt, height_pt) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (doc_version_id, 21, "oob", 0, 612.0, 792.0),
            )

        with self.connect() as conn:
            project_id, actor_id = self.make_project(conn)
            with patch("lab_db.ingest._insert_pages", side_effect=insert_out_of_bounds_page):
                with self.assertRaises(psycopg.Error):
                    ingest_document(
                        conn,
                        self.store,
                        project_id=project_id,
                        actor_id=actor_id,
                        logical_id="doc-rollback-trigger",
                        data=self.pdf_bytes,
                        inspection=inspection,
                        pages=pages,
                    )

            with conn.cursor(row_factory=dict_row) as cur:
                for table in (
                    "operations",
                    "resource_versions",
                    "document_versions",
                    "audit_events",
                    "outbox_events",
                ):
                    cur.execute(
                        f"SELECT count(*) AS cnt FROM {table} WHERE project_id = %s",
                        (project_id,),
                    )
                    self.assertEqual(cur.fetchone()["cnt"], 0, f"expected 0 rows in {table}")

                cur.execute("SELECT count(*) AS cnt FROM pages p JOIN document_versions dv ON dv.id = p.document_version_id WHERE dv.project_id = %s", (project_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)

                cur.execute(
                    "SELECT state, sha256 FROM artifacts WHERE project_id = %s",
                    (project_id,),
                )
                art_rows = cur.fetchall()

        self.assertEqual(len(art_rows), 1)
        self.assertEqual(art_rows[0]["state"], "committed")
        self.assertEqual(art_rows[0]["sha256"], self.pdf_sha256)


if __name__ == "__main__":
    unittest.main()
