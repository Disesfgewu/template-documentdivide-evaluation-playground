# Reproducible non-root Docker runtime image for LAB API HTTP service (SPEC R24, R25, §16.4; Refs #3).
# Digest-pinned Python 3.12 slim Debian Bookworm base (multi-arch index).
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH="/app/services/api:/app/db:/app/contracts:/app/services/parser:/app/scoring"

# Dedicated unprivileged non-root user and group (UID/GID 10001:10001)
RUN groupadd -g 10001 lab \
    && useradd -u 10001 -g lab -s /usr/sbin/nologin -M -d /nonexistent lab \
    && mkdir -p /var/lib/lab/artifacts \
    && chown -R 10001:10001 /var/lib/lab/artifacts \
    && chmod 755 /var/lib/lab/artifacts

WORKDIR /app

# Locked dependencies: API dependencies + parser dependencies (for PDF native validation/extraction) + contracts
COPY services/api/requirements.lock /app/services/api/requirements.lock
COPY services/parser/requirements.lock /app/services/parser/requirements.lock
COPY contracts/requirements.lock /app/contracts/requirements.lock
RUN pip install --no-cache-dir \
    -r /app/services/api/requirements.lock \
    -r /app/services/parser/requirements.lock \
    -r /app/contracts/requirements.lock

# Selective runtime source only (no test payloads, private agentKit, or development tools)
COPY db/lab_db /app/db/lab_db
COPY contracts/lab_contracts /app/contracts/lab_contracts
COPY services/parser/lab_parser /app/services/parser/lab_parser
COPY scoring/lab_scoring /app/scoring/lab_scoring
COPY services/api/lab_api /app/services/api/lab_api

WORKDIR /app/services/api

USER 10001:10001

EXPOSE 8000
VOLUME ["/var/lib/lab/artifacts"]

ENTRYPOINT ["uvicorn", "--factory"]
CMD ["lab_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
