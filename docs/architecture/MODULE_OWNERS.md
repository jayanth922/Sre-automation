# Module owners and reachability (P10)

Every shipped feature must have a clear entry point. Optional CLI/benchmark
modules are listed separately so they cannot be mistaken for product surface.

## Production entry points

| Owner module | Entry point | Notes |
|--------------|-------------|-------|
| `sre_agent.agent_runtime` | FastAPI `app` | HTTP + WebSocket surface |
| `sre_agent.job_worker` | `python -m sre_agent.job_worker` | Postgres lease-backed durable job worker; invokes `sre_agent.incident_runner.run_incident_investigation` |
| `sre_agent.sandbox_worker` | `python -m sre_agent.sandbox_worker` | Temporal worker; runs `sre_agent.sandbox_workflow.CodeFixVerificationWorkflow`, the log-based recovery oracle for AI-proposed code fixes |
| `sre_agent.graph_builder` | LangGraph compile | Canonical investigation graph |
| `sre_agent.multi_agent_langgraph` | `create_multi_agent_system` | Specialist wiring |
| `sre_agent.api.v1.*` | `/api/v1/*` routers | Tenant-scoped REST |
| `backend.routers.auth` | `/auth/*` | Login / session |
| `dashboard/app` | Next.js App Router | Operator UI |

`sre_agent.agent_runtime_tasks` is listed as a reachability root
(`scripts/check_module_reachability.py`) but is not an active entry point: it
is a quarantined forwarding shim (`DeprecationWarning` + call-through) that
exists only so old imports don't hard-fail. New work must call
`sre_agent.incident_runner.run_incident_investigation` directly.

## Intentionally experimental (CLI / benchmarks only)

These are **not** product features. They may be imported by benchmarks or
`python -m` CLIs, but must not gain UI affordances without a product owner.

| Module | Allowed entry | Owner |
|--------|---------------|-------|
| `sre_agent.actor_runtime` | `AGENT_RUNTIME` CLI / terminal agent | Benchmarks |
| `sre_agent.terminal_agent` | `python -m sre_agent.terminal_agent` | Benchmarks |
| `sre_agent.code_sandbox` | `SANDBOX_BACKEND` for actors | Benchmarks |
| `sre_agent.toolsets` | ITBench adapter registry | Benchmarks |

## Archived

See [`archive/experimental/README.md`](../../archive/experimental/README.md).

## Drift prevention

- `scripts/check_module_reachability.py` — fails if a top-level `sre_agent/*.py`
  module is neither reachable from production entry points nor explicitly listed
  as experimental/archived.
- `tests/test_module_reachability.py` — runs the checker in CI.
- `tests/test_ui_backend_contract.py` — dashboard components must not call APIs
  the backend does not expose; orphaned components must not linger.
