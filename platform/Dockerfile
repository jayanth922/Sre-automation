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
    && pip install --no-cache-dir uv

# Copy dependency manifest and install
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Anthropic (Claude) client. Kept as a separate image layer instead of in the
# locked graph so `uv sync --frozen` stays reproducible; only needed when
# LLM_PROVIDER=anthropic. Groq / openai_compatible work without it.
RUN uv pip install --no-cache "langchain-anthropic>=0.2.0"

# Temporal client/worker SDK. Same image doubles as the sandbox workflow
# worker (see deploy/helm/sentinel/templates/temporal-worker.yaml) with a
# different entrypoint, so it needs this even though the API process itself
# never imports sre_agent.sandbox_workflow directly.
RUN uv pip install --no-cache "temporalio>=1.7.0"

# Copy application code — only platform-relevant modules
COPY sre_agent/ ./sre_agent/
COPY backend/   ./backend/
COPY alembic.ini ./

ENV PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTINEL_CODE_SHA=${SENTINEL_CODE_SHA}

LABEL org.opencontainers.image.revision=${SENTINEL_CODE_SHA}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/ping || exit 1

CMD ["uv", "run", "uvicorn", "sre_agent.agent_runtime:app", "--host", "0.0.0.0", "--port", "8080"]
