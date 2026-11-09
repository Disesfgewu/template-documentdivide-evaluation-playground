# Reproducible non-root Docker runtime image for LAB parser HTTP service (SPEC R24, R25; Refs #3).
# Digest-pinned Python 3.12 slim Debian Bookworm base (multi-arch index).
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH="/app/services/parser"

# Dedicated unprivileged non-root user and group (UID/GID 10001:10001)
RUN groupadd -g 10001 lab \
    && useradd -u 10001 -g lab -s /usr/sbin/nologin -M -d /nonexistent lab \
    && mkdir -p /var/lib/lab/artifacts \
    && chown -R 10001:10001 /var/lib/lab/artifacts \
    && chmod 755 /var/lib/lab/artifacts

WORKDIR /app

# Locked dependencies: parser dependencies (pypdf, pypdfium2, fastapi, uvicorn, pydantic)
COPY services/parser/requirements.lock /app/services/parser/requirements.lock
RUN pip install --no-cache-dir \
    -r /app/services/parser/requirements.lock

# Selective runtime source only (parser package)
COPY services/parser/lab_parser /app/services/parser/lab_parser

WORKDIR /app/services/parser

USER 10001:10001

EXPOSE 8100
VOLUME ["/var/lib/lab/artifacts"]

ENTRYPOINT ["uvicorn", "--factory"]
CMD ["lab_parser.main:create_app", "--host", "0.0.0.0", "--port", "8100"]
