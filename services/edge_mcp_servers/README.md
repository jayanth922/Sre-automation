# Edge MCP services

These services expose infrastructure evidence and tightly controlled mutation
tools through the Model Context Protocol. They do not contain the reasoning
loop; `src/sre_agent` selects and calls them.

The local stack includes Kubernetes, Prometheus, Loki, GitHub read, Notion
runbooks, Kubernetes executor, GitHub execution, and sandbox services. Each
server has an independent Dockerfile under [`mcp_servers/`](mcp_servers/).

## Local use

```bash
cp services/edge_mcp_servers/.env.example services/edge_mcp_servers/.env
bash services/edge_mcp_servers/start.sh
```

The main `scripts/dev/start.sh` helper performs this setup automatically.

Every HTTP/SSE request requires the shared MCP bearer token. Published local
ports bind to loopback. Cluster credentials, GitHub credentials, and Notion
credentials can be relayed per tenant and cluster; process-level values are
single-tenant fallbacks.

Read operations and mutations are separated. Executor and GitHub-write tools
validate their own allowlists and guardrails, while the control plane's
mutation gateway remains the authoritative final authorization boundary.

Stop the services with `bash services/edge_mcp_servers/stop.sh`. An opt-in live
connectivity check is available at `scripts/smoke/mcp_servers.py`; it may make
paid model calls and is not part of default verification.

The Meridian reference client is isolated under
[`examples/meridian`](../../examples/meridian/).
