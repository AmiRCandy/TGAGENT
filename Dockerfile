# tgagent container image.
#
# Two-stage build: a builder that compiles wheels, and a slim runtime that
# carries neither a compiler nor the build cache.
#
# The runtime starts as root, hands the data directory to an unprivileged user,
# and drops to that user before exec'ing the application — see
# docker/entrypoint.py for why that order is forced on us by platform-attached
# volumes. Nothing in the application ever runs as root.

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
      org.opencontainers.image.source="https://github.com/AmiRCandy/tgagent"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TGAGENT_DATA_DIR=/data \
    TGAGENT_LOGGING__FORMAT=json

COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.py /opt/entrypoint.py

# The container has no Docker socket, so the docker sandbox backend cannot work
# from inside it. The subprocess backend is the correct choice here: the
# container itself is the isolation boundary.
ENV TGAGENT_SANDBOX__BACKEND=subprocess

# The account the application runs as. The entrypoint reads this, so overriding
# it with `-e TGAGENT_RUN_AS=root` is possible but means running as root.
ENV TGAGENT_RUN_AS=tgagent

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tgagent \
    && mkdir -p /data \
    && chown -R tgagent:tgagent /data \
    && chmod 700 /data

WORKDIR /home/tgagent

# Deliberately no VOLUME instruction.
#
# It buys nothing here and actively causes problems. Managed platforms (Railway,
# Render, Fly) attach storage from their own configuration and ignore it; and on
# plain Docker it silently creates an *anonymous* volume when someone forgets
# `-v`, so a container that looks fine keeps its session and database in a volume
# nobody knows the name of, and a `docker run --rm` throws the login away. An
# explicit `-v tgagent-data:/data` is clearer, and required either way.
#
# /data holds the session file, the database, and downloaded media. Back it up
# like a credential store, because that is what it is.

# Starts as root purely so the entrypoint can take ownership of an attached
# volume; it drops to TGAGENT_RUN_AS before the application starts.
USER root
ENTRYPOINT ["python", "/opt/entrypoint.py"]

# `serve` runs the scheduler, and the Telegram control bridge alongside it when
# control.enabled is set — one process for both. Sign in before deploying:
# either interactively (docker run -it --rm -v tgagent-data:/data tgagent login)
# or by supplying TGAGENT_SESSION_B64. See docs/deploy-railway.md.
#CMD ["tgagent","listen"]
CMD ["sleep","infinity"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD tgagent version || exit 1
