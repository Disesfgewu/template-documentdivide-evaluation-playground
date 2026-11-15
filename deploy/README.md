# LAB Runtime Docker Images

This directory provides reproducible, unprivileged (non-root) Docker runtime images for the FastAPI LAB API service (`deploy/api.Dockerfile`) and the Python parser HTTP service (`deploy/parser.Dockerfile`).

Base image: digest-pinned Debian Bookworm Python 3.12 slim (`python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`).

## Images and Ports

| Service | Dockerfile | Default Port | UID:GID | Entrypoint |
|---|---|---|---|---|
| LAB API | `deploy/api.Dockerfile` | `8000` | `10001:10001` (`lab:lab`) | `uvicorn --factory lab_api.main:create_app --host 0.0.0.0 --port 8000` |
| Parser HTTP | `deploy/parser.Dockerfile` | `8100` | `10001:10001` (`lab:lab`) | `uvicorn --factory lab_parser.main:create_app --host 0.0.0.0 --port 8100` |

Both containers execute `uvicorn` using shell-free JSON exec array syntax (`ENTRYPOINT ["uvicorn", "--factory"]`), drop privileges to unprivileged non-root user `10001:10001`, and run with dropped capabilities (`--cap-drop=ALL --security-opt=no-new-privileges`).

## Build

From repository root:

```bash
docker build -f deploy/api.Dockerfile -t lab-api:dev .
docker build -f deploy/parser.Dockerfile -t lab-parser:dev .
```

The root `.dockerignore` denies all files by default, selectively allowing only the required Dockerfiles, exact lockfiles, and application runtime source packages. Secrets, private staff tooling, tests, fixtures, frontend, worker, and logs are denied everywhere.

## Runtime Volume & Configuration

- **Artifact Volume**: `/var/lib/lab/artifacts`
  - Writable volume owned by `10001:10001`.
  - For persistent storage, use a Docker named volume (e.g. `docker volume create lab-artifacts` mounted to `/var/lib/lab/artifacts`), or an explicitly owned host bind mount directory with permissions `0750` (owned by UID/GID 10001:10001) or `0770` with a shared group via `--group-add <gid>`. World-writable permissions (`chmod 777`) violate `LocalArtifactStore` security preconditions and must not be used.
- **Required Environment Variables**:
  - API:
    - `LAB_ENV`: `dev` or `test`
    - `LAB_PROVIDER_MODE`: `mock`
    - `LAB_DATABASE_URL`: `postgresql://...`
    - `LAB_ARTIFACT_ROOT`: `/var/lib/lab/artifacts`
    - `LAB_AUTH_SECRET`: signing secret for bearer tokens
  - Parser:
    - `LAB_PARSER_ENV`: `dev` or `test`
    - `LAB_PARSER_ARTIFACT_ROOT`: `/var/lib/lab/artifacts`

## Run Example (with Resource Limits, Read-Only Root, and Dropped Capabilities)

```bash
# 1. Start PostgreSQL + pgvector
docker compose -f deploy/compose.dev.yml up -d --wait

# 2. Named volume for artifacts
docker volume create lab-artifacts

# 3. Start API container
docker run -d --name lab-api \
  --network lab-dev_default \
  -p 127.0.0.1:8000:8000 \
  --cpus="1.0" --memory="512m" --pids-limit=100 \
  --read-only \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=64m \
  -v lab-artifacts:/var/lib/lab/artifacts \
  -e LAB_ENV=dev \
  -e LAB_PROVIDER_MODE=mock \
  -e LAB_DATABASE_URL=postgresql://lab:lab-dev-only@db:5432/lab \
  -e LAB_ARTIFACT_ROOT=/var/lib/lab/artifacts \
  -e LAB_AUTH_SECRET=dev-secret-only \
  lab-api:dev

# 4. Start Parser container
docker run -d --name lab-parser \
  --network lab-dev_default \
  -p 127.0.0.1:8100:8100 \
  --cpus="1.0" --memory="512m" --pids-limit=100 \
  --read-only \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=64m \
  -v lab-artifacts:/var/lib/lab/artifacts \
  -e LAB_PARSER_ENV=dev \
  -e LAB_PARSER_ARTIFACT_ROOT=/var/lib/lab/artifacts \
  lab-parser:dev
```

## Boundaries and Current Scope

- **Dev / Test Runtime Only**: Current application configuration rejects `LAB_ENV=prod` with `LAB_PROVIDER_MODE=mock`, and rejects `LAB_PROVIDER_MODE=real` because real provider adapters are not yet implemented (T84 pending).
- **Parser HTTP Service**: Currently exposes liveness `/healthz`, storage readiness `/readyz`, and capabilities `/v1/capabilities`. Parser inference endpoints are not yet offered (SPEC §16, #8).
- **Other Components Not Covered**: Go workflow worker (`services/worker`), parser inference workers, React web UI (`web`), and full multi-container orchestration are not containerized in this increment.
- **Alembic / Database Migrations**: Alembic migration containerization is tracked separately under PR #77 (`deploy/Dockerfile.migrate`); this increment does not duplicate or alter database schema or migrations.
- **Tracking**: Refs #3 (#3 remains open).

## Runtime smoke

After starting the existing Compose database, run from the repository root:

```bash
export LAB_TEST_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab
python -m deploy.runtime_smoke --report-dir /tmp/runtime-smoke-evidence
```

The smoke requires Linux Docker and the `lab-dev_default` network supplied by `compose.dev.yml`. It creates a disposable database and named artifact volume, uses random loopback-only published service ports, runs both images as UID10001, performs actual PDFium rendering, and verifies authenticated upload/download, cross-project denial and persistence across restart. Cleanup removes its containers, volume and database; a cleanup failure is a failed smoke. It does not publish images. Local absence of Docker is blocked (exit3), not a passed container build. CI retains the measured JSON report.

The smoke currently calls the migration interface already on main; #77 changes its implementation to Alembic. Recheck Docker context and migration packaging when integrating #77; this runtime context allow-list does not establish compatibility with its migration image.
