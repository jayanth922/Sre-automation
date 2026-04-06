# Edge MCP Servers

A collection of Model Context Protocol (MCP) servers deployed as standalone Docker Compose services. The SaaS Platform communicates with these servers to securely read metrics, logs, and state without opening sensitive internal APIs directly.

---

## What it contains

```
edge_mcp_servers/
├── docker-compose.yaml   # Runs all the MCP servers and exposes their ports
├── .env.example          # Configuration template
└── mcp_servers/
    ├── k8s_real/         # Reads pods, events, logs, deployments
    ├── prometheus_real/  # Executes PromQL queries
    ├── loki_real/        # Executes LogQL queries
    ├── github_real/      # Reads commits, PRs, issues, and repository files
    └── runbooks_local/   # Looks up local Markdown runbooks
```

---

## Setup

```bash
cp .env.example .env
```

Edit `.env` to configure connections to your internal tools:

```env
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100

GITHUB_TOKEN=ghp_...
GITHUB_REPO=your-org/your-repo
```

Runbooks are stored in the repository root `runbooks/` folder and mounted into the local runbooks MCP server.

The GitHub MCP can also read repository source files when the target client lives in GitHub. Use the file tools to inspect code paths directly during incident analysis.

Start the stack:

```bash
docker compose up -d --build
```

---

## Connecting the SaaS Platform

Once the MCP servers are running, map their exposed ports to the `.env` file of the SaaS Platform (`sre_agent/`):

```env
MCP_K8S_URI=http://localhost:3000/sse
MCP_METRICS_URI=http://localhost:3001/sse
MCP_LOGS_URI=http://localhost:3002/sse
MCP_GITHUB_URI=http://localhost:3003/sse
MCP_RUNBOOKS_URI=http://localhost:4004/sse
```

The SaaS LangGraph agent will connect dynamically to discover tools and query data.

To smoke test the local runbooks server directly, run the existing layer-2 test in `testing/test_layer2.py`; it already validates SSE connectivity plus `search_runbooks` and `get_runbook_content`.
