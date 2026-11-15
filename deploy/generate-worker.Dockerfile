# Image of the LAB document.generate worker (P-D, Refs #7): the Python poller plus the lab-gen Go child.
#   stage 1 builds lab-gen from services/worker/agentflow against the pinned public agentKit commit
#   stage 2 is the same digest-pinned Python base as the API image, with the API runtime requirements only.
# The image holds NO key: LAB_GEN_API_KEY is injected at run time (docs/LAB_GEN.md, scripts/demo.sh generate).
FROM golang:1.27.1-bookworm AS lab-gen
# agentKit is public; the pinned commit (the repository's gitlink) is fetched here, because third_party is excluded from
# the build context on purpose (.dockerignore). scripts/demo.sh passes AGENTKIT_COMMIT from `git rev-parse HEAD:third_party/agentkit`.
ARG AGENTKIT_COMMIT
ARG AGENTKIT_REPO=https://github.com/Disesfgewu/agentkit.git
RUN test -n "$AGENTKIT_COMMIT" || { echo "AGENTKIT_COMMIT build arg is required" >&2; exit 1; } \
    && git init -q /src/third_party/agentkit && cd /src/third_party/agentkit \
    && git fetch -q --depth 1 "$AGENTKIT_REPO" "$AGENTKIT_COMMIT" && git checkout -q FETCH_HEAD
COPY services/worker/agentflow /src/services/worker/agentflow
WORKDIR /src/services/worker/agentflow
RUN CGO_ENABLED=0 GOTOOLCHAIN=local go build -trimpath -o /out/lab-gen ./cmd/lab-gen

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH="/app/services/api:/app/db:/app/contracts:/app/services/parser:/app/scoring" \
    LAB_GEN_BIN=/usr/local/bin/lab-gen \
    LAB_WORKER_HEARTBEAT_FILE=/tmp/worker.heartbeat

RUN groupadd -g 10001 lab && useradd -u 10001 -g lab -s /usr/sbin/nologin -M -d /nonexistent lab \
    && mkdir -p /var/lib/lab/artifacts && chown -R 10001:10001 /var/lib/lab/artifacts && chmod 755 /var/lib/lab/artifacts

WORKDIR /app
COPY services/api/requirements.lock /app/services/api/requirements.lock
COPY services/parser/requirements.lock /app/services/parser/requirements.lock
COPY contracts/requirements.lock /app/contracts/requirements.lock
RUN pip install --no-cache-dir -r /app/services/api/requirements.lock -r /app/services/parser/requirements.lock -r /app/contracts/requirements.lock

COPY db/lab_db /app/db/lab_db
COPY contracts/lab_contracts /app/contracts/lab_contracts
COPY services/parser/lab_parser /app/services/parser/lab_parser
COPY services/api/lab_api /app/services/api/lab_api
COPY scoring/lab_scoring /app/scoring/lab_scoring
COPY --from=lab-gen /out/lab-gen /usr/local/bin/lab-gen

WORKDIR /app/services/api
USER 10001:10001
ENTRYPOINT ["python", "-m", "lab_api.generate_worker_main"]
CMD ["--interval", "5"]
