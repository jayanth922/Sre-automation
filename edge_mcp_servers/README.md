# Edge MCP Servers

This directory contains the edge relay layer that exposes real infrastructure and knowledge sources to the platform through MCP. It is the bridge between the SaaS control plane and the customer-target observability and code context.

The key thing to understand is that this folder does not contain the reasoning engine. It contains the evidence layer. The platform talks to these services when it needs to inspect the cluster, read metrics, search logs, inspect code history, or retrieve runbook guidance.

## What Runs Here

The edge compose stack starts these MCP servers:

- Kubernetes operations and cluster state.
- Prometheus metrics access.
- Loki log access.
- GitHub repository intelligence.
- Local markdown runbooks.

The edge layer does not run the AI itself. It only serves tool access back to the platform so the LangGraph runtime can reason with live data.

## Start And Stop

Preferred startup:

```bash
cd edge_mcp_servers
docker compose up -d --build
```

Helper script:

```bash
./start.sh
./stop.sh
```

The startup script expects a `.env` file in this directory and warns if `CLUSTER_TOKEN` is not set to a platform-issued value.

## Ports And Service Roles

- Kubernetes MCP: http://localhost:4000
- Prometheus MCP: http://localhost:4001
- Loki MCP: http://localhost:4002
- GitHub MCP: http://localhost:4003
- Runbooks MCP: http://localhost:4004

Each server container listens on port 3000 internally and is published on a distinct host port by the compose file. The platform chooses which tool to call based on the kind of evidence it needs.

## Configuration Notes

- `PROMETHEUS_URL` and `LOKI_URL` should point at the host-exposed observability stack for the customer target.
- `GITHUB_TOKEN` and `GITHUB_REPO` are required for the GitHub MCP server.
- The Kubernetes server mounts the local kubeconfig and expects Docker Desktop or another reachable Kubernetes context.
- The runbooks server reads markdown files from the repository-backed runbooks directory.
- The relay stack uses the same host networking assumptions as the target client and the platform compose file.

## Operational Flow

When the platform is investigating an incident, the sequence usually looks like this:

1. The dashboard sends a follow-up or the runtime receives an alert.
2. The agent asks the relevant MCP server for evidence.
3. The MCP server returns structured data.
4. The graph reasons over that data and writes the result into the incident transcript.

This is why the edge layer matters. Without it, the agent would be forced to reason from static code or synthetic examples instead of live infrastructure state.

## Related Docs

- [mcp_servers/README.md](mcp_servers/README.md)
- [../Target_Client/README.md](../Target_Client/README.md)
- [../README.md](../README.md)