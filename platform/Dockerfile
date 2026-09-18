# =============================================================================
# Platform API Server — SRE Agent Control Plane + AI Brain
# =============================================================================
# Runs the FastAPI server, LangGraph agent, and Alembic migrations.
# All AI reasoning (LLM calls, OODA loop) happens in this container.
#
# Build context: project root (../)
#   docker build -f platform/Dockerfile -t sre-platform-api .
# =============================================================================

FROM python:3.12-slim-bookworm

# Release builders pass the source revision. An omitted/unknown value is kept
# explicit so run manifests become non-comparable instead of inventing a SHA.
ARG SENTINEL_CODE_SHA=unknown

WORKDIR /app

# Install system deps (wget for healthcheck, git for pip installs)
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.6.14"

# Copy dependency manifest and install
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra temporal --extra anthropic

# The Temporal client/worker SDK comes from the locked `temporal` extra above.
# This image doubles as the sandbox workflow worker (see
# deploy/helm/sentinel/templates/temporal-worker.yaml), so both entrypoints use
# exactly the same SDK build.

# Copy application code — only platform-relevant modules
COPY sre_agent/ ./sre_agent/
COPY backend/   ./backend/
COPY alembic.ini ./

# Freeze the exact Python runtime copied into this image, then prove the
# shared API/Temporal-worker imports are complete while the image is built.
# Both entrypoints re-run the same check before accepting work.
RUN SENTINEL_CODE_SHA="${SENTINEL_CODE_SHA}" \
    /app/.venv/bin/python -m sre_agent.runtime_preflight \
    --write-manifest /app/.sentinel-runtime.json
RUN SENTINEL_CODE_SHA="${SENTINEL_CODE_SHA}" \
    SENTINEL_RUNTIME_MANIFEST=/app/.sentinel-runtime.json \
    /app/.venv/bin/python -m sre_agent.runtime_preflight --role worker

ENV PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTINEL_CODE_SHA=${SENTINEL_CODE_SHA} \
    SENTINEL_RUNTIME_MANIFEST=/app/.sentinel-runtime.json

LABEL org.opencontainers.image.revision=${SENTINEL_CODE_SHA}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/ping || exit 1

CMD ["uv", "run", "uvicorn", "sre_agent.agent_runtime:app", "--host", "0.0.0.0", "--port", "8080"]
