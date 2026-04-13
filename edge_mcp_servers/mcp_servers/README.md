# MCP Tool Servers

This folder contains the concrete MCP server implementations used by the edge relay stack. Each subfolder is a standalone service with its own Dockerfile, Python dependencies, and `server.py` entry point.

These servers are intentionally narrow. Each one is a specialist evidence source rather than a generic API server, which is what makes the LangGraph runtime easier to reason about.

## Shared Pattern

Every server in this folder follows the same shape:

- `server.py` defines a FastMCP server.
- `Dockerfile` builds the container image.
- `requirements.txt` captures the Python dependencies for that tool server.

The servers are designed to be run individually or through [../docker-compose.yaml](../docker-compose.yaml).

## Server Roles

- [k8s_real/](k8s_real/) exposes Kubernetes operations.
- [prometheus_real/](prometheus_real/) exposes Prometheus queries and metrics lookup.
- [loki_real/](loki_real/) exposes log search and log context lookup.
- [github_real/](github_real/) exposes GitHub repository intelligence.
- [runbooks_local/](runbooks_local/) exposes repository-backed operational runbooks.

## Runtime Expectations

- These services are read-only by default from the perspective of the edge stack.
- The platform consumes them over the Model Context Protocol.
- The concrete tool names differ by server, but the service boundary stays consistent: connect, query, and return structured evidence.
- The services are designed to return structured payloads that the supervisor and specialist agents can summarize without guessing.

## How To Think About This Folder

This folder is where the assistant’s “eyes and ears” are implemented. If the runtime is going to ask “what changed in the cluster?”, “what errors are in the logs?”, or “what did the last deploy do?”, the answer usually comes from one of these servers.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)