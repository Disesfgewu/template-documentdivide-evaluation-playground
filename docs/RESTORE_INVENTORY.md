# Restored Artifact Inventory Verification

`tools.governance.restore_inventory` provides bounded, read-only verification of a supplied committed artifact inventory against local restored bytes (Refs #14). It verifies that every committed artifact entry in the inventory exists under `<store_root>/sha256/<prefix>/<digest>`, is a bounded regular file, has exact matching byte length and content SHA-256 digest, and traverses no symbolic links in any path component.

## Architecture and Contracts

- **Store layout**: Expects the content-addressed filesystem layout used by the local artifact store:
  `<store_root>/sha256/<prefix>/<digest>` where `<prefix>` is the first 2 hex digits of the lowercase 64-hex `<digest>`.
- **Supported Inventory Formats**:
  1. JSON envelope: `{"format": "lab-artifact-inventory-1", "artifacts": [...]}`
  2. JSON array: `[{"object_key": "sha256/...", "size_bytes": 1234}, ...]`
  3. JSONL stream / file: newline-delimited JSON objects matching exported database rows.
  4. In-memory Python iterables of `Mapping[str, Any]` or `InventoryItem` instances.
- **Entry schema**:
  - `object_key` (or `key`): required `sha256/<2-hex-prefix>/<64-hex-digest>`.
  - `size_bytes` (or `size`): non-negative integer within `max_file_bytes`.
  - `sha256`: optional or checked; if present, must match key digest.
  - `state`: optional; if present, MUST be `'committed'`. Staged or tombstoned states fail closed.
  - Optional metadata: `object_version`, `id`, `project_id`, `mime`, `retention`, `created_at`, `committed_at`.
  - Unrecognized fields: fail closed.

## Security and Integrity Guarantees

1. **Strictly read-only**: Zero filesystem mutations, creations, renames, unlinks, or deletions. No temporary files are written to the store.
2. **Descriptor-relative `O_NOFOLLOW` traversal**:
   - Starting from root `/`, every ancestor component and the `store_root` directory itself are traversed with `O_NOFOLLOW` and verified with `follow_symlinks=False`.
   - Store subdirectories (`sha256`, `<prefix>`) and the object file itself are opened descriptor-relative (`dir_fd`) with `O_NOFOLLOW`.
   - Fails closed immediately on any symbolic link in any component of the path.
3. **Non-regular file defense**:
   - Files are opened with `O_NONBLOCK` and verified with `stat.S_ISREG`.
   - Named pipes (FIFOs), character/block devices, sockets, and directories fail closed without blocking or hanging reader threads.
4. **Untrusted input bounding**:
   - Default limits: 32 MiB max inventory file, 10,000 items per batch, 512 MiB max per file, 10 GiB aggregate verified bytes.
   - Streaming hash calculation: chunked 64 KiB reads avoid loading large objects into memory.
   - Duplicate rejection: duplicate JSON fields and duplicate `object_key` entries fail closed.
5. **Canonical directory identity verification**:
   - Directory device (`st_dev`) and inode (`st_ino`) of `store_root` are checked before and after verification to detect directory substitution races.

## Operator CLI Usage

```bash
# Verify an inventory file against a restored store directory
python3 -m tools.governance.restore_inventory --inventory /path/to/inventory.jsonl --store-root /var/data/artifacts

# Positional syntax
python3 -m tools.governance.restore_inventory /path/to/inventory.json /var/data/artifacts

# Machine-readable JSON output
python3 -m tools.governance.restore_inventory --inventory /path/to/inventory.json --store-root /var/data/artifacts --json
```

Exit status:
- `0`: All inventory items verified successfully.
- `1`: Verification failed (corruption, missing object, symlink, hash mismatch, or malformed input).
- `2`: Invalid CLI arguments or syntax error.

## Operational Scope and Boundaries

- **Not a database snapshot**: This tool verifies restored filesystem bytes against a supplied inventory. It does not authenticate that the supplied inventory is a consistent database snapshot. The operator must obtain the committed artifact inventory from an authenticated, consistent database snapshot.
- **No coordinated recovery**: Coordinated backup/restore orchestration between PostgreSQL and the artifact store is not performed by this primitive.
- **No authorization or tenant isolation**: Does not enforce project-level multi-tenant isolation or access control.
- **Foundation scope only (Refs #14)**: Does not complete full T79, T80, or issue #14 acceptance.
