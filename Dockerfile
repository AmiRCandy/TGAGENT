# tgagent container image.
#
# Two-stage build: a builder that compiles wheels, and a slim runtime that
# carries neither a compiler nor the build cache. The runtime runs as a
# non-root user, and the data directory (which holds the session file — an
# authenticated credential) is a volume with restrictive ownership.

# ─────────────────────────────────────────────── builder ────────────────────
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# cryptg is a Rust extension; without a toolchain pip would silently fall back
# to the pure-Python crypto and media transfers would be several times slower.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install ".[anthropic,openai,speedups]"

# ─────────────────────────────────────────────── runtime ────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="tgagent" \
      org.opencontainers.image.description="Autonomous AI agent for a personal Telegram account" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/tgagent/tgagent"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TGAGENT_DATA_DIR=/data \
    TGAGENT_LOGGING__FORMAT=json

COPY --from=builder /opt/venv /opt/venv

# The container has no Docker socket, so the docker sandbox backend cannot work
# from inside it. The subprocess backend is the correct choice here: the
# container itself is the isolation boundary.
ENV TGAGENT_SANDBOX__BACKEND=subprocess

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tgagent \
    && mkdir -p /data \
    && chown -R tgagent:tgagent /data \
    && chmod 700 /data

USER tgagent
WORKDIR /home/tgagent

# Holds the session file, the database, and downloaded media. Back it up like a
# credential store, because that is what it is.
VOLUME ["/data"]

# `serve` runs the scheduler in the foreground. Sign in first with an
# interactive run:  docker run -it --rm -v tgagent-data:/data tgagent login
ENTRYPOINT ["tgagent"]
CMD ["serve"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD tgagent version || exit 1
