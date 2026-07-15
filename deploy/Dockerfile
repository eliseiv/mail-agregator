# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Mail Aggregator — unified multi-stage Dockerfile
#
# Two final stages share an identical wheel/venv layer:
#   - target=api     -> FastAPI + gunicorn/uvicorn workers, listens on :8080
#   - target=worker  -> APScheduler entrypoint (no listening port)
#
# Compose selects the target via `build.target: api|worker`. Architect's
# spec (docs/05-modules.md sec.0) names two Dockerfiles (api.Dockerfile,
# worker.Dockerfile) but explicitly allows devops to pick the layout
# ("Devops-агент может выбрать другой layout"); a single Dockerfile with
# two targets gives the same isolation with one less file to keep in sync.
#
# Dependency manager: uv (Astral). Reasoning:
#   - Reproducible (uv.lock committed by backend agent).
#   - 10-100x faster than pip in CI.
#   - Compiles to a vanilla venv that the runtime stage just copies.
# Backend agent MUST create pyproject.toml + uv.lock at repo root.
# See deploy/README.md for the contract.
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.4.27
ARG APP_UID=1000
ARG APP_GID=1000

# ---------------------------------------------------------------------------
# Stage 1: builder — produce a frozen venv from pyproject.toml + uv.lock
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv is shipped as a static binary. Pin the version for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.4.27 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Build deps for native wheels (argon2-cffi, cryptography). Removed in
# the final stage — only the compiled venv is copied forward.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        pkg-config \
 && rm -rf /var/lib/apt/lists/*

# Copy ONLY dependency manifests first to maximise layer cache hits.
# README.md is referenced by pyproject.toml (`readme = "README.md"`); hatchling
# refuses to build the project wheel without it, so it must be present
# alongside pyproject.toml.
COPY pyproject.toml uv.lock README.md ./

# Install runtime dependencies into a self-contained venv at /opt/venv.
# --frozen: refuses to update uv.lock; CI fails fast if lock is stale.
# --no-install-project: we copy source code separately to keep deps cached.
RUN uv venv /opt/venv \
 && uv sync \
        --frozen \
        --no-dev \
        --no-install-project

# Copy application source. shared/ is imported by both api and worker.
COPY backend ./backend
COPY worker ./worker
COPY shared ./shared

# Install the project itself (non-editable) so `backend`, `worker`,
# `shared` packages live in site-packages alongside their deps.
# pyproject.toml declares all three as wheel packages (hatchling).
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime-base — common slim runtime, non-root, /opt/venv on PATH
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime-base

ARG APP_UID
ARG APP_GID

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app"

# tini  -> proper PID 1, forwards signals to gunicorn/python so SIGTERM
#          triggers graceful shutdown instead of being eaten.
# The api/worker healthchecks use python stdlib (urllib / os.stat) so the
# runtime image needs no extra HTTP-client deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid ${APP_GID} app \
 && useradd  --system --uid ${APP_UID} --gid ${APP_GID} \
             --home-dir /app --shell /usr/sbin/nologin app

# Pull in the venv built in stage 1.
COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app

# Copy app source (backend, worker, shared, migrations).
COPY --chown=app:app backend       ./backend
COPY --chown=app:app worker        ./worker
COPY --chown=app:app shared        ./shared
COPY --chown=app:app migrations    ./migrations
COPY --chown=app:app alembic.ini   ./alembic.ini

# Defensive: never store core dumps. Mail master key lives in process
# memory and dumps would leak it (docs/06-security.md sec.1.6).
# ulimit is enforced by the runtime via the entrypoint, but we also strip
# the SUID bit on every binary in the image as belt-and-braces.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

USER app

# tini reaps zombies and forwards SIGTERM/SIGINT properly.
ENTRYPOINT ["/usr/bin/tini", "--"]

# ---------------------------------------------------------------------------
# Stage 3a: api — FastAPI behind gunicorn/uvicorn workers
# ---------------------------------------------------------------------------
FROM runtime-base AS api

EXPOSE 8080

# Healthcheck matches docs/07-deployment.md sec.3 — pure stdlib so we don't
# need curl in the api image. /healthz is the contract from the backend
# spec (modules.md). Compose-level healthcheck overrides this in dev for
# uniformity, but baking it in lets `docker run` users get it for free.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", \
         "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3); \
sys.exit(0 if r.status==200 else 1)"]

# gunicorn config from docs/07-deployment.md sec.5:
#   2 uvicorn workers, 60s request timeout, 30s graceful shutdown,
#   bind to all interfaces inside the container (only docker network exposes it).
CMD ["gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "2", \
     "--bind", "0.0.0.0:8080", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "backend.app.main:app"]

# ---------------------------------------------------------------------------
# Stage 3b: worker — APScheduler entrypoint, no ports
# ---------------------------------------------------------------------------
FROM runtime-base AS worker

# Healthcheck per docs/07-deployment.md sec.3: worker touches
# /tmp/worker_alive every 30s; we fail if mtime is older than 6 minutes.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", \
         "import time,os,sys; \
m=os.stat('/tmp/worker_alive').st_mtime; \
sys.exit(0 if (time.time()-m)<360 else 1)"]

CMD ["python", "-m", "worker.app.main"]
