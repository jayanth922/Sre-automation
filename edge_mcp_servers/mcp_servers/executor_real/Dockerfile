# Customer MCP Tool Server — Executor (cluster mutations: restart/scale/patch/rollback)
FROM python:3.12-slim

WORKDIR /app

# System deps + kubectl (used by rollback_deployment)
RUN apt-get update && apt-get install -y curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm -f kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY mcp_servers/executor_real/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_servers/executor_real/guardrails.py .
COPY mcp_servers/executor_real/server.py .
COPY mcp_auth.py .
COPY relay_credentials.py .

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -I http://127.0.0.1:3000 || exit 1

CMD ["python", "server.py"]
