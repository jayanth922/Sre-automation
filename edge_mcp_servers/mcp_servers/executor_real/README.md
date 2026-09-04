# Executor MCP Server

The **write** counterpart to the read-only `k8s_real` server. This is where the
agent's ACT phase becomes real cluster operations. It exposes a narrow,
allow-listed set of remediation tools over MCP (SSE), published on host port
**4005**.

## Tools

| Tool | Effect | `kubectl` equivalent |
| --- | --- | --- |
| `executor_health` | Connectivity + safety envelope | — |
| `restart_deployment` | Rolling restart | `kubectl rollout restart deployment/<n>` |
| `scale_deployment` | Scale to N (≥ floor) | `kubectl scale deployment/<n> --replicas=N` |
| `patch_resource_limits` | Set container mem/cpu limits | `kubectl set resources ...` |
| `rollback_deployment` | Undo to previous revision | `kubectl rollout undo deployment/<n>` |
| `recreate_pod` | Delete one pod; controller recreates it | `kubectl delete pod/<n>` |

## Safety model (defense in depth)

The agent-side Policy Gate already decides whether an action may run
autonomously (severity × reversibility). This server enforces a **second,
independent** envelope that the LLM cannot widen:

- **Dry-run by default.** Every tool takes `dry_run` (default `true`). Dry-run
  maps to the Kubernetes API server-side dry-run (`dryRun=All`) — the apiserver
  validates but persists nothing.
- **Guardrails** (`guardrails.py`, operator-owned env vars):
  - `EXECUTOR_ALLOWED_NAMESPACES` (default `demo-app`) — refuse anything outside.
  - `EXECUTOR_MIN_REPLICAS` (default `1`) — refuse scale below the floor
    (scale-to-0 / outage guard).
  - Action allow-list: `restart`, `scale`, `rollback`, `patch_resource_limits`,
    `recreate_pod`.
- **Least-privilege RBAC.** Grant this server only `patch`/`get` on
  `deployments` (+ `deployments/scale`), and `delete`/`get` on `pods`, in the
  demo namespace. Do not give it cluster-admin.

## Configuration

- `HOST` / `HTTP_PORT` — bind address / port (container listens on 3000).
- `KUBERNETES_API_SERVER_HOST` — Docker Desktop host alias (as with `k8s_real`).
- `EXECUTOR_ALLOWED_NAMESPACES`, `EXECUTOR_MIN_REPLICAS` — safety envelope.

## Run

Built as the `mcp-executor` service by [`../../docker-compose.yaml`](../../docker-compose.yaml):

```bash
cd edge_mcp_servers
docker compose up -d --build mcp-executor
```

The agent discovers it via `MCP_EXECUTOR_URI` (e.g. `http://localhost:4005/sse`).
