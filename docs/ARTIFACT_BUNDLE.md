# Offline artifact bundle primitive

`tools.governance.artifact_backup` exports an explicit list of SHA-256 object
keys using the same `sha256/<first-two>/<digest>` layout as the proposed #5
local store. It rejects symlinks in the selected paths and verifies every
object against its key before returning a deterministic JSON bundle. Restore
validates every entry and its digest before creating a new destination; it
never replaces an existing destination. The two functions enforce fixed counts,
per-object, aggregate and encoded size limits. They operate on bytes and do not
invoke an archive extractor or execute content.

This is a local, in-memory primitive, not a complete backup system. The caller
must obtain the object list from an authenticated, consistent database snapshot,
secure the bundle against disclosure and tampering, and coordinate database and
artifact restore. A checksum alone does not authenticate a bundle or prove the
selection is complete. Source and destination parents must be private and must
not be concurrently modified. Restore creates files in its new destination
before returning; no reader should consume it before success. Cleanup after a
write error is best effort, and writes are not fsynced. There is no deployment,
project authorization, encryption, database restore or recovery verification
integration in this increment. These remain required for #14 and release.
