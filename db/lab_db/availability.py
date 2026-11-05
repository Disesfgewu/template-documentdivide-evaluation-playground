"""Resource version and submission availability checks (SPEC 10.2, 17.4 / T52)."""

from dataclasses import dataclass
from typing import Literal
import uuid

from lab_db.storage import LocalArtifactStore, StorageCorruption


@dataclass(frozen=True)
class Availability:
    status: Literal["available", "declared_only", "unavailable"]
    reasons: tuple[str, ...]


def _check_artifact(
    state: str,
    object_key: str,
    store: LocalArtifactStore | None,
) -> list[str]:
    reasons: list[str] = []
    if state == "tombstoned":
        reasons.append("artifact_tombstoned")
    if store is not None:
        try:
            store.verify(object_key)
        except StorageCorruption:
            reasons.append("artifact_corrupt")
        except (FileNotFoundError, OSError):
            reasons.append("artifact_missing")
    return reasons


def describe(
    conn,
    project_id: uuid.UUID,
    resource_ids: list[uuid.UUID],
    store: LocalArtifactStore | None = None,
) -> dict[uuid.UUID, Availability]:
    """Describe availability for a batch of resource_versions in ``project_id``.

    Raises ``LookupError`` if any id is not a resource_version in ``project_id``
    (without revealing whether it exists in another project).
    """
    if not resource_ids:
        return {}

    unique_ids = list(dict.fromkeys(resource_ids))
    rows = conn.execute(
        """
        SELECT rv.id,
               mr.visibility,
               a.id AS artifact_id,
               a.state AS artifact_state,
               a.object_key
          FROM resource_versions rv
          LEFT JOIN method_revisions mr
                 ON mr.project_id = rv.project_id
                AND mr.resource_id = rv.id
          LEFT JOIN document_versions dv
                 ON dv.project_id = rv.project_id
                AND dv.resource_id = rv.id
          LEFT JOIN artifacts a
                 ON a.project_id = dv.project_id
                AND a.id = dv.artifact_id
         WHERE rv.project_id = %s
           AND rv.id = ANY(%s)
        """,
        (project_id, unique_ids),
    ).fetchall()

    by_id = {row[0]: row for row in rows}
    if len(by_id) != len(unique_ids):
        missing = [rid for rid in unique_ids if rid not in by_id]
        raise LookupError(f"resource_versions not found in project: {missing[0]}")

    out: dict[uuid.UUID, Availability] = {}
    for rid in resource_ids:
        if rid in out:
            continue
        _, mr_visibility, artifact_id, artifact_state, object_key = by_id[rid]
        if artifact_id is not None:
            reasons = _check_artifact(artifact_state, object_key, store)
            if reasons:
                out[rid] = Availability("unavailable", tuple(reasons))
            else:
                out[rid] = Availability("available", ())
        elif mr_visibility == "declared_only":
            out[rid] = Availability("declared_only", ("method_internals_not_observed",))
        else:
            out[rid] = Availability("available", ())
    return out


def describe_submission(
    conn,
    project_id: uuid.UUID,
    submission_id: uuid.UUID,
    store: LocalArtifactStore | None = None,
) -> Availability:
    """Describe availability of a submission payload and its run's method visibility.

    Raises ``LookupError`` if the submission does not belong to a run in ``project_id``.
    """
    row = conn.execute(
        """
        SELECT a.state,
               a.object_key,
               mr.visibility
          FROM submissions s
          JOIN runs r
            ON r.id = s.run_id
          JOIN artifacts a
            ON a.project_id = r.project_id
           AND a.id = s.payload_artifact_id
          JOIN method_revisions mr
            ON mr.project_id = r.project_id
           AND mr.id = r.method_revision_id
         WHERE s.id = %s
           AND r.project_id = %s
        """,
        (submission_id, project_id),
    ).fetchone()

    if row is None:
        raise LookupError(f"submission not found in project: {submission_id}")

    artifact_state, object_key, mr_visibility = row
    artifact_reasons = _check_artifact(artifact_state, object_key, store)
    reasons = list(artifact_reasons)
    if mr_visibility == "declared_only":
        reasons.append("method_internals_not_observed")

    if artifact_reasons:
        return Availability("unavailable", tuple(reasons))
    if mr_visibility == "declared_only":
        return Availability("declared_only", tuple(reasons))
    return Availability("available", ())
