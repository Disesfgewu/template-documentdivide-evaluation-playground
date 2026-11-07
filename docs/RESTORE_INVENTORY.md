# Restored Artifact Inventory Verification

`tools.governance.restore_inventory` provides bounded, read-only verification of a supplied committed artifact inventory against local restored bytes (Refs #14). It verifies that every committed artifact entry in the inventory exists under `<store_root>/sha256/<prefix>/<digest>`, is a bounded regular file, has exact matching byte length and content SHA-256 digest, and traverses no symbolic links in any path component.

## Architecture and Contracts

- **Store layout**: Expects the content-addressed filesystem layout used by the local artifact store:
  `<store_root>/sha256/<prefix>/<digest>` where `<prefix>` is the first 2 hex digits of the lowercase 64-hex `<digest>`.
- **Supported Inventory Formats**:
  1. JSON envelope: `{"format": "lab-artifact-inventory-1", "artifacts": [...]}`
  2. JSON array: `[{"object_key": "sha256/...", "size_bytes": 1234}, ...]`
  3. JSONL stream / file: newline-delimited JSON objects matching exported database rows.
  4. In-memory Python iterables of `Mapping[str, Any]` or `InventoryItem` instances (bounded iteration).
- **Entry schema**:
  - `object_key` (or `key`): required `sha256/<2-hex-prefix>/<64-hex-digest>`. Contradictory aliases (`object_key` != `key`) fail closed.
  - `size_bytes` (or `size`): non-negative integer within `max_file_bytes`. Contradictory aliases (`size_bytes` != `size`) fail closed.
  - `sha256`: optional or checked; if present, must match key digest.
  - `state`: optional; if present, MUST be `'committed'`. Explicit `null`/`None` or non-committed states fail closed.
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
4. **Untrusted input bounding and memory safety**:
   - Default limits: 32 MiB max inventory file, 10,000 items per batch, 512 MiB max per file, 10 GiB aggregate verified bytes.
   - Bounded iteration: consumes at most `max_items + 1` entries, defending against infinite adversarial generator streams.
   - Pre-read aggregate bounds: total expected bytes across all items are checked before reading any files.
   - Streaming hash calculation: chunked 64 KiB reads avoid loading large objects into memory.
   - Duplicate rejection: duplicate JSON fields and duplicate `object_key` entries fail closed.
5. **Canonical directory & object identity verification (race defense)**:
   - For every verified artifact, the tool canonically re-traverses from `/` through `store_root`, `sha256`, and `<prefix>` down to `<digest>`, verifying that device (`st_dev`) and inode (`st_ino`) match the opened descriptors and that the canonical file is present and regular.
   - If an intermediate directory (such as `<prefix>`) is renamed and replaced with an empty directory during or after opening, verification fails closed immediately.

## Threat Model and Post-Operation Boundary

- **In-process atomic verification**: The tool verifies canonical directory and file identities (`st_dev`, `st_ino`) and file content during verification, preventing directory-swap and symlink-substitution races during execution.
- **Quiescent store required**: Exclude all writers before verification begins and until its result is used. Individual files are checked sequentially; a writer can alter an earlier file during later checks or change bytes between hashing and the identity check. The result is not an atomic filesystem snapshot. Protect the store and its ancestors with DAC/MAC and use a trusted, isolated restore target.
- **Post-operation DAC/MAC boundary**: Like any user-space verification tool, once file descriptors are closed and the process exits, the tool cannot prevent an attacker with filesystem write access from subsequently mutating, deleting, or swapping directories or files. Deployments must enforce strict operating system discretionary and mandatory access controls (DAC/MAC, e.g. mode 0700/0750 owned exclusively by the runtime service user) on `<store_root>` and all ancestor paths to protect the store after verification.

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
