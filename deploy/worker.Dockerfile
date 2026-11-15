# Reproducible non-root Docker runtime image for the LAB document.parse worker (Refs #7; SPEC R24, R25).
# Runs `python -m lab_api.parse_worker_main` with the pinned parser library (unstructured 0.27.36, CPU only).
# Digest-pinned Python 3.12 slim Debian Bookworm base, the same as the API and parser images.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH="/app/services/api:/app/db:/app/contracts:/app/services/parser" \
    LAB_WORKER_HEARTBEAT_FILE=/tmp/worker.heartbeat

# Dedicated unprivileged non-root user and group (UID/GID 10001:10001); the worker only reads artifacts
# and the parser child works in a private temporary directory under /tmp (a tmpfs when the root is read-only).
RUN groupadd -g 10001 lab \
    && useradd -u 10001 -g lab -s /usr/sbin/nologin -M -d /nonexistent lab \
    && mkdir -p /var/lib/lab/artifacts \
    && chown -R 10001:10001 /var/lib/lab/artifacts \
    && chmod 755 /var/lib/lab/artifacts

WORKDIR /app

# One pinned lock (API + parser + contracts + db + unstructured and its dependencies; see the lock's header).
COPY deploy/worker.requirements.lock /app/deploy/worker.requirements.lock
RUN pip install --no-cache-dir -r /app/deploy/worker.requirements.lock

# Selective runtime source only (no tests, no private agentKit, no development tools, no secrets).
COPY db/lab_db /app/db/lab_db
COPY contracts/lab_contracts /app/contracts/lab_contracts
COPY services/parser/lab_parser /app/services/parser/lab_parser
COPY services/api/lab_api /app/services/api/lab_api

WORKDIR /app/services/api

USER 10001:10001

VOLUME ["/var/lib/lab/artifacts"]

# Configuration is environment only (LAB_DATABASE_URL, LAB_ARTIFACT_ROOT, LAB_AUTH_SECRET, ...). Liveness: the
# worker rewrites LAB_WORKER_HEARTBEAT_FILE every pass; the probe never opens a database connection.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "lab_api.parse_worker_main", "--check-heartbeat", "/tmp/worker.heartbeat", "--max-age", "120"]

ENTRYPOINT ["python", "-m", "lab_api.parse_worker_main"]
CMD ["--concurrency", "1", "--interval", "5"]
