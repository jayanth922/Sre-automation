# Customer MCP Tool Server — Notion (Runbook queries)
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 3000

ENV MCP_TRANSPORT=sse
CMD ["python", "server.py"]
