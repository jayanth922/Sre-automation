# Sandbox MCP Tool Server — ephemeral K8s Jobs for verifying AI-generated code fixes
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY mcp_servers/sandbox_real/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_servers/sandbox_real/guardrails.py .
COPY mcp_servers/sandbox_real/server.py .
COPY mcp_auth.py .

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -I http://127.0.0.1:3000 || exit 1

CMD ["python", "server.py"]
