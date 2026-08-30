# Customer MCP Tool Server — GitHub exec (code-change remediation: revert PRs)
FROM python:3.12-slim

WORKDIR /app

# System deps + GitHub CLI (used for the live revert/comment path)
RUN apt-get update && apt-get install -y curl git \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

COPY mcp_servers/github_exec/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_servers/github_exec/guardrails.py .
COPY mcp_servers/github_exec/server.py .
COPY mcp_auth.py .
COPY relay_credentials.py .

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -I http://127.0.0.1:3000 || exit 1
CMD ["python", "server.py"]
