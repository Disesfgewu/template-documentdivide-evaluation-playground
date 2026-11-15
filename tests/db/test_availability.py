"""T52 (SPEC 10.2, 17.4): resource version and submission availability checks."""

import inspect
import tempfile
from pathlib import Path
import uuid

from lab_db.artifacts import tombstone
from lab_db.availability import Availability, describe, describe_submission
from lab_db.repo import Change, record_change
from lab_db.storage import LocalArtifactStore, store_and_register

from .base import FreshDatabase, h
from .test_runs import RunFixture


def _store_pdf(conn, store: LocalArtifactStore, project_id: uuid.UUID, data: bytes):
    """Call store_and_register across signature variations and return (artifact_id, sha256)."""
    sig = inspect.signature(store_and_register)
    params = list(sig.parameters.keys())
    kwargs = {}
    if "mime" in params:
        kwargs["mime"] = "application/pdf"
    elif "content_type" in params:
        kwargs["content_type"] = "application/pdf"
    if "retention" in params:
        kwargs["retention"] = "restricted"
    if "commit" in params:
        kwargs["commit"] = True
    if "state" in params:
        kwargs["state"] = "committed"

    # Match positional arguments: (conn, store, project_id, ...) or (conn, project_id, store, ...)
    if len(params) >= 3 and params[1] in ("project_id", "project"):
        pos = [conn, project_id, store]
    else:
        pos = [conn, store, project_id]

    for p in params[len(pos):]:
        if p in kwargs:
            continue
        if p in ("data", "content", "payload", "body", "raw"):
            kwargs[p] = data
        elif p in ("object_key", "key"):
            kwargs[p] = f"docs/{uuid.uuid4().hex}.pdf"
        elif p in ("object_version", "version"):
            kwargs[p] = "v1"

    res = store_and_register(*pos, **kwargs)
    if isinstance(res, uuid.UUID):
        art_id = res
    elif isinstance(res, tuple):
        art_id = next(x for x in res if isinstance(x, uuid.UUID))
    elif hasattr(res, "id"):
        art_id = res.id
    elif hasattr(res, "artifact_id"):
        art_id = res.artifact_id
    else:
        raise TypeError(f"Unexpected return from store_and_register: {res!r}")

    row = conn.execute(
        "SELECT state, sha256 FROM artifacts WHERE id = %s",
        (art_id,),
    ).fetchone()
    if row[0] == "staged":
        conn.execute(
            "UPDATE artifacts SET state = 'committed', committed_at = now() WHERE id = %s",
            (art_id,),
        )
    return art_id, row[1]


def _make_resource_version(
    conn,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    resource_type: str,
    logical_id: str,
    content_hash: str,
) -> uuid.UUID:
    """Create a resource_versions row via record_change (with SQL fallback if needed)."""
    rv_id = uuid.uuid4()
    sig = inspect.signature( Change)
    c_params = set(sig.parameters.keys())
    c_kwargs = {}
    mapping = {
        "project_id": project_id,
        "actor_id": actor_id,
        "created_by": actor_id,
        "actor": actor_id,
        "resource_id": rv_id,
        "id": rv_id,
        "resource_type": resource_type,
        "logical_id": logical_id,
        "revision": 1,
        "content_hash": content_hash,
        "c14n_version": "v1",
        "action": "create",
        "reason": "test",
        "outcome": "succeeded",
    }
    for k, v in mapping.items():
        if k in c_params:
            c_kwargs[k] = v
    try:
        change = Change(**c_kwargs)
        res = record_change(conn, change)
        if isinstance(res, uuid.UUID):
            rv_id = res
        elif hasattr(res, "resource_id") and isinstance(res.resource_id, uuid.UUID):
            rv_id = res.resource_id
        elif hasattr(res, "id") and isinstance(res.id, uuid.UUID):
            rv_id = res.id
    except TypeError:
        pass

    exists = conn.execute(
        "SELECT 1 FROM resource_versions WHERE id = %s",
        (rv_id,),
    ).fetchone()
    if not exists:
        conn.execute(
            """
            INSERT INTO resource_versions
                (id, project_id, resource_type, logical_id, revision, content_hash, c14n_version, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, 'v1', %s)
            """,
            (rv_id, project_id, resource_type, logical_id, content_hash, actor_id),
        )
    return rv_id


def _call_tombstone(conn, project_id: uuid.UUID, actor_id: uuid.UUID, artifact_id: uuid.UUID, reason: str) -> None:
    sig = inspect.signature(tombstone)
    params = list(sig.parameters.keys())
    kwargs = {}
    if "project_id" in params and params.index("project_id") > 1:
        kwargs["project_id"] = project_id
    if "actor_id" in params:
        kwargs["actor_id"] = actor_id
    elif "actor" in params:
        kwargs["actor"] = actor_id

    if len(params) >= 3 and params[1] == "project_id":
        tombstone(conn, project_id, artifact_id, reason, **kwargs)
    else:
        tombstone(conn, artifact_id, reason, **kwargs)


class AvailabilityTests(FreshDatabase):
    def _doc_resource_and_artifact(self, conn, doc_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        row = conn.execute(
            "SELECT resource_id, artifact_id FROM document_versions WHERE id = %s",
            (doc_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0], row[1]

    def _method_resource(self, conn, method_id: uuid.UUID) -> uuid.UUID:
        row = conn.execute(
            "SELECT resource_id FROM method_revisions WHERE id = %s",
            (method_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_document_available_and_tombstoned(self):
        with self.connect() as conn:
            fx = RunFixture(self, conn)
            doc_rv, art_id = self._doc_resource_and_artifact(conn, fx.doc)
            method_rv = self._method_resource(conn, fx.method)

            res = describe(conn, fx.p, [doc_rv, method_rv])
            self.assertEqual(list(res.keys()), [doc_rv, method_rv])
            self.assertEqual(res[doc_rv], Availability("available", ()))
            self.assertEqual(
                res[method_rv],
                Availability("declared_only", ("method_internals_not_observed",)),
            )

            _call_tombstone(conn, fx.p, fx.a, art_id, "retention expired")
            res_after = describe(conn, fx.p, [doc_rv])
            self.assertEqual(
                res_after[doc_rv],
                Availability("unavailable", ("artifact_tombstoned",)),
            )

    def test_document_store_verify_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir, self.connect() as conn:
            fx = RunFixture(self, conn)
            store = LocalArtifactStore(Path(tmpdir))
            raw = b"%PDF-1.4 test document bytes"

            with conn.transaction():
                art_id, sha = _store_pdf(conn, store, fx.p, raw)
                doc_rv = _make_resource_version(
                    conn, fx.p, fx.a, "document", f"doc-{uuid.uuid4().hex[:8]}", sha
                )
                conn.execute(
                    """
                    INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count)
                    VALUES (%s, %s, %s, %s, %s, 1)
                    """,
                    (uuid.uuid4(), fx.p, doc_rv, art_id, sha),
                )

            # Intact on disk -> available
            self.assertEqual(
                describe(conn, fx.p, [doc_rv], store=store)[doc_rv],
                Availability("available", ()),
            )

            # Find the file in tmpdir and corrupt its contents
            files = [p for p in Path(tmpdir).rglob("*") if p.is_file()]
            self.assertEqual(len(files), 1)
            blob_path = files[0]
            blob_path.write_bytes(b"corrupted pdf content")

            corrupt_avail = describe(conn, fx.p, [doc_rv], store=store)[doc_rv]
            self.assertEqual(corrupt_avail.status, "unavailable")
            self.assertIn("artifact_corrupt", corrupt_avail.reasons)

            # Remove the file from disk -> artifact_missing
            blob_path.unlink()
            missing_avail = describe(conn, fx.p, [doc_rv], store=store)[doc_rv]
            self.assertEqual(missing_avail.status, "unavailable")
            self.assertIn("artifact_missing", missing_avail.reasons)

    def test_submission_declared_only_and_tombstoned(self):
        with self.connect() as conn:
            fx = RunFixture(self, conn)
            run_id = fx.run()
            att_id = fx.attempt(run_id, fx.q)
            sub_id = fx.submission(run_id, att_id, fx.q)

            avail = describe_submission(conn, fx.p, sub_id)
            self.assertEqual(
                avail,
                Availability("declared_only", ("method_internals_not_observed",)),
            )

            # Tombstone the submission payload artifact -> unavailable, keeping both reasons
            art_id = conn.execute(
                "SELECT payload_artifact_id FROM submissions WHERE id = %s",
                (sub_id,),
            ).fetchone()[0]
            _call_tombstone(conn, fx.p, fx.a, art_id, "retention expired")

            avail_tomb = describe_submission(conn, fx.p, sub_id)
            self.assertEqual(avail_tomb.status, "unavailable")
            self.assertIn("artifact_tombstoned", avail_tomb.reasons)
            self.assertIn("method_internals_not_observed", avail_tomb.reasons)

    def test_cross_project_lookup_error(self):
        with self.connect() as conn:
            fx = RunFixture(self, conn)
            other_p, _ = self.make_project(conn)
            doc_rv, _ = self._doc_resource_and_artifact(conn, fx.doc)

            run_id = fx.run()
            att_id = fx.attempt(run_id, fx.q)
            sub_id = fx.submission(run_id, att_id, fx.q)

            with self.assertRaises(LookupError):
                describe(conn, other_p, [doc_rv])

            with self.assertRaises(LookupError):
                describe(conn, fx.p, [uuid.uuid4()])

            with self.assertRaises(LookupError):
                describe_submission(conn, other_p, sub_id)

            with self.assertRaises(LookupError):
                describe_submission(conn, fx.p, uuid.uuid4())
