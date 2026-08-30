# Customer MCP Tool Server — Loki (Log queries)
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY mcp_servers/loki_real/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_servers/loki_real/server.py .
COPY mcp_auth.py .
COPY relay_credentials.py .

EXPOSE 3000

ENV MCP_TRANSPORT=sse
CMD ["python", "server.py"]
