# MCP server implementations

Each subdirectory is an independently buildable MCP service with a narrow tool
surface:

- `k8s_real`, `prometheus_real`, and `loki_real`: infrastructure evidence.
- `github_real` and `runbooks_notion`: code and operational knowledge.
- `executor_real` and `github_exec`: guarded mutations.
- `sandbox_real`: isolated verification work.

Shared request authentication and relayed tenant credentials live one directory
above. Read and write tools remain separate, and empty mutation allowlists fail
closed.

See [the edge-service overview](../README.md).
