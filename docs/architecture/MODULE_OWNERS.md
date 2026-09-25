# Module ownership and reachability

Every shipped Python module must be reachable from a production entry point or
explicitly classified as a benchmark/experimental surface.

## Production entry points

- `sre_agent.agent_runtime`: FastAPI HTTP and WebSocket application.
- `sre_agent.job_worker`: Postgres lease-backed durable job worker.
- `sre_agent.sandbox_worker`: Temporal remediation/sandbox worker.
- `sre_agent.graph_builder`: canonical investigation graph.
- `sre_agent.incident_remediation_workflow`: process-safe remediation flow.
- `sre_agent.api.v1.*`: authenticated, tenant-scoped REST routes.
- `backend.routers.auth`: claim and session endpoints.
- `apps/dashboard/app`: Next.js operator routes.
- `services/edge_mcp_servers/mcp_servers/*`: independent MCP service images.

`sre_agent.agent_runtime_tasks` is a compatibility forwarding shim, not a new
entry point. New code should call `incident_runner.run_incident_investigation`
directly.

## Benchmark and experimental surfaces

`sre_agent.actor_runtime`, `sre_agent.terminal_agent`, and
`sre_agent.toolsets` are allowed benchmark/CLI roots. Historical experiments
live under [`archive/experimental/`](../../archive/experimental/).

## Drift prevention

- `scripts/ci/check_module_reachability.py` fails when a top-level agent module
  is neither reachable nor explicitly allowed.
- `tests/test_module_reachability.py` runs that checker and verifies active UI
  contracts.
- `tests/test_console_wiring.py` and integration contract tests prevent the
  console from relying on missing API routes.
