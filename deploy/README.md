# LAB Runtime Docker Images

This directory provides reproducible, unprivileged (non-root) Docker runtime images for the FastAPI LAB API service (`deploy/api.Dockerfile`) and the Python parser HTTP service (`deploy/parser.Dockerfile`).

Base image: digest-pinned Debian Bookworm Python 3.12 slim (`python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`).

## Images and Ports

| Service | Dockerfile | Default Port | UID:GID | Entrypoint |
|---|---|---|---|---|
| LAB API | `deploy/api.Dockerfile` | `8000` | `10001:10001` (`lab:lab`) | `uvicorn --factory lab_api.main:create_app --host 0.0.0.0 --port 8000` |
| Parser HTTP | `deploy/parser.Dockerfile` | `8100` | `10001:10001` (`lab:lab`) | `uvicorn --factory lab_parser.main:create_app --host 0.0.0.0 --port 8100` |

Both containers execute `uvicorn` using shell-free JSON exec array syntax (`ENTRYPOINT ["uvicorn", "--factory"]`), and drop privileges to unprivileged non-root user `10001:10001`.

## Build

From repository root:

```bash
docker build -f deploy/api.Dockerfile -t lab-api:dev .
docker build -f deploy/parser.Dockerfile -t lab-parser:dev .
```

The root `.dockerignore` excludes version control (`.git`), private modules/staff tooling (`.agy-staff`), development caches, credentials, tests, fixtures, docs, frontend sources, and worker code.

## Runtime Volume & Configuration

- **Artifact Volume**: `/var/lib/lab/artifacts`
  - Writable volume owned by `10001:10001`.
  - When mounting a host directory, ensure the host directory is writable by UID 10001 (e.g. `chmod 777 host-dir` or owned by `10001:10001`).
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

## Run Example (with Resource Limits & Read-Only Root)

```bash
# 1. Start PostgreSQL + pgvector
docker compose -f deploy/compose.dev.yml up -d --wait

# 2. Host artifact mount directory
mkdir -p /tmp/lab-artifacts && chmod 777 /tmp/lab-artifacts

# 3. Start API container
docker run -d --name lab-api \
  --network host \
  --cpus="1.0" --memory="512m" --pids-limit=100 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,size=64m \
  -v /tmp/lab-artifacts:/var/lib/lab/artifacts \
  -e LAB_ENV=dev \
  -e LAB_PROVIDER_MODE=mock \
  -e LAB_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab \
  -e LAB_ARTIFACT_ROOT=/var/lib/lab/artifacts \
  -e LAB_AUTH_SECRET=dev-secret-only \
  lab-api:dev

# 4. Start Parser container
docker run -d --name lab-parser \
  --network host \
  --cpus="1.0" --memory="512m" --pids-limit=100 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,size=64m \
  -v /tmp/lab-artifacts:/var/lib/lab/artifacts \
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
