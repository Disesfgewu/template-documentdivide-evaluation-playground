# PostgreSQL Backup and Disposable-Database Restore Verification

`lab_api.postgres_backup` provides a scoped operator workflow for Issue #14
(SPEC §10.2, R24, T79, A29 foundation) to create a verified `pg_dump`
custom-format archive (`--format=custom`) and validate that archive by
restoring it only into an explicitly identified, distinct, disposable, and
verified-empty PostgreSQL target database.

## Operator Prerequisites

1. **Client tools**: `pg_dump` and `pg_restore` matching the target PostgreSQL
   server major version (PostgreSQL 17 per [compose.dev.yml](../deploy/compose.dev.yml))
   must be installed and executable on `PATH` (or passed via explicit absolute
   non-symlink binary paths).
2. **Pre-provisioned disposable target database**: The restore verifier **never**
   executes `CREATE DATABASE` or `DROP DATABASE` and never passes `--create` or
   `--clean` to `pg_restore`. Before running restore verification, the operator
   must provision an empty, isolated disposable database whose name starts with
   one of the required disposable prefixes (`lab_restore_`, `lab_verify_`,
   `disposable_`, `restore_verify_`, or `tmp_restore_`), whose database name is
   distinct from the source database name, and grant the restore role
   permission to create objects and required extensions (such as `vector`).
3. **Private parent directory**: The parent directory for `<backup_dir>` must
   already exist, be owned by the operator with private permissions (`0700`
   recommended), must not be a symlink on any path component, and must not be
   concurrently modified by other processes. `<backup_dir>` itself must not
   exist prior to `create_postgres_backup`.

## CLI and Python Usage

```bash
# 1. Create a custom-format backup archive and companion manifest.json
export LAB_BACKUP_SOURCE_DATABASE_URL="postgresql://lab:lab-dev-only@127.0.0.1:55432/lab"
(cd services/api && python -m lab_api pg-backup /var/backups/lab/run-20261107-01)

# 2. Verify archive directory layout, manifest schema, PGDMP header, size, and SHA-256
(cd services/api && python -m lab_api pg-verify-archive /var/backups/lab/run-20261107-01)

# 3. Verify restore into a pre-created disposable empty PostgreSQL database
export LAB_RESTORE_TARGET_DATABASE_URL="postgresql://lab:lab-dev-only@127.0.0.1:55432/lab_restore_check_01"
(cd services/api && python -m lab_api pg-verify-restore \
    /var/backups/lab/run-20261107-01 \
    lab_restore_check_01)
```

Python API (`services/api/lab_api/postgres_backup.py`):

- `create_postgres_backup(source_database_url, backup_dir, *, limits=None, pg_dump_bin="pg_dump") -> BackupManifest`
- `verify_backup_directory(backup_dir, *, limits=None) -> BackupManifest`
- `verify_postgres_restore(backup_dir, target_database_url, *, confirm_disposable_target_database, source_database_url=None, limits=None, pg_restore_bin="pg_restore") -> RestoreVerificationReport`

## Security and Safety Guarantees

- **No shell interpolation**: Subprocesses are launched with `shell=False`,
  `cwd="/"`, `close_fds=True`, and `start_new_session=True` using fixed
  argument vectors.
- **No credentials in process arguments, logs, or output**: Database URLs,
  passwords, hostnames, and raw driver/tool stderr are never placed in CLI
  arguments, returned reports, `manifest.json`, or `PostgresBackupError`
  messages. Credentials are passed to `pg_dump`/`pg_restore` exclusively via a
  minimal subprocess environment (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
  `PGPASSWORD`, `PGSSLMODE`, `PGCONNECT_TIMEOUT`, `PGOPTIONS`). Arbitrary libpq
  URL query parameters (such as `passfile`, `service`, `sslrootcert`, or
  `options`) are rejected (`unsupported_url_parameter`).
- **Destructive / production restore prevention**:
  - `confirm_disposable_target_database` is required and must exactly match the
    target database parsed from `target_database_url`.
  - Target database names are rejected unless they start with an explicit
    disposable prefix (`lab_restore_`, `lab_verify_`, `disposable_`,
    `restore_verify_`, `tmp_restore_`); reserved/production names (`lab`,
    `postgres`, `prod`, `production`, `main`, `default`, `template0`,
    `template1`) are unconditionally refused.
  - Restore is refused (`target_matches_source`) if the target database name or
    its SHA-256 fingerprint matches either the explicit `source_database_url`
    database name or `source_database_sha256` recorded in `manifest.json`
    **regardless of hostname** (preventing bypasses via DNS aliases, CNAMEs, or
    alternate loopback addresses such as `127.0.0.2`), as well as if
    `source_endpoint_sha256` matches.
  - Before invoking `pg_restore`, the target database is inspected over a live
    `psycopg` connection and fails closed (`target_not_empty`,
    `target_in_use`, or `target_endpoint_mismatch`) if the server is in
    recovery, if any other backend is connected to the target database in
    `pg_stat_activity`, or if any extra schema, non-extension user
    table/view/sequence/index, user routine, user type/domain, event trigger,
    or non-allowlisted extension exists.
- **Symlink, path, and descriptor-staging safety (no pathname re-open)**:
  Relative paths and `.`/`..` segments are rejected. Every directory component
  is traversed with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, and archive/manifest
  files are opened with `O_NOFOLLOW` and verified as single-link regular files
  (`st_nlink == 1`). Archive hashing is streamed in fixed 64 KiB chunks so
  multi-hundred-MB or multi-GB archives are never buffered whole in RAM. During
  `verify_postgres_restore`, the verified archive descriptor is streamed into a
  private unlinked temporary file descriptor (`O_TMPFILE` / unlinked descriptor)
  and passed to `pg_restore --list --format=custom` and `pg_restore --format=custom`
  via `stdin`, so `pg_restore` never re-opens a mutable filesystem path after
  verification (preventing post-verification file replacement or symlink swap
  races).
- **Bounded time, output, and cleanup**: `BackupLimits` bounds wall clock time
  (default 60s), connection timeout (default 5s), statement/lock timeouts,
  archive size (default 256 MiB), manifest size (64 KiB), and captured tool
  stdout/stderr (default 256 KiB). On timeout, output overflow, or tool
  failure, the subprocess group is killed (`SIGKILL`) and any newly created
  `<backup_dir>` is removed best-effort.

## Explicit Limitations and Non-Claims

- **Digest is not authentication, and no SQL sandbox is claimed**:
  `manifest.json` records an unkeyed SHA-256 digest solely to detect accidental
  bit-rot, truncation, or local file mismatch. An unkeyed digest does **not**
  authenticate an untrusted backup bundle, and restoring a PostgreSQL archive
  via `pg_restore` executes arbitrary SQL/DDL/functions under the target
  database role (`no sandbox` is provided). Operators must only restore
  archives produced and stored within a trusted boundary.
- **Target database emptiness TOCTOU window**: `verify_postgres_restore` checks
  that the target database is empty and has zero other backends in
  `pg_stat_activity` before launching `pg_restore` in a separate client
  session. Because `pg_restore` opens its own database connection after the
  pre-check connection closes, a concurrent external client with target DB
  credentials could connect in that window. Operators must dedicate the
  disposable target database to a single verification run with no concurrent
  writers.
- **Not a coordinated cross-store recovery point**: An independent `pg_dump`
  archive — even when paired with an offline artifact bundle — does **not**
  provide a single atomic or transactionally coordinated snapshot across
  PostgreSQL and artifact storage. Concurrent writes between or during database
  dump and artifact collection can leave database rows and artifact files out of
  sync unless the operator quiesces writes or uses storage/WAL coordination.
- **No automatic database lifecycle or full T79/A29 completion**: This increment
  does not create or drop databases, manage backup retention schedules, encrypt
  archives at rest, or verify end-to-end score-to-PDF lineage and measured
  deployment RPO/RTO across the full LAB schema (#5, #7, #14, #15).
