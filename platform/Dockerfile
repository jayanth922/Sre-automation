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

WORKDIR /app

# Install system deps (wget for healthcheck, git for pip installs)
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Copy dependency manifest and install
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code — only platform-relevant modules
COPY sre_agent/ ./sre_agent/
COPY backend/   ./backend/
COPY alembic.ini ./
# Runbook corpus — the console's runbook catalog reads this at /app/runbooks
COPY edge_mcp_servers/mcp_servers/runbooks_local/runbooks ./runbooks

ENV PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/ping || exit 1

CMD ["uv", "run", "uvicorn", "sre_agent.agent_runtime:app", "--host", "0.0.0.0", "--port", "8080"]
