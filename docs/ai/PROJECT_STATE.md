# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10 remain open as PRs #3–#12. R02 durable jobs is PR #13. R03 namespace
enforcement is implemented on `codex/r03-namespace-enforcement`.

## Current architecture and invariants
- Investigation jobs use durable Postgres leases (R02 / PR #13).
- Configured cluster namespace is required in API/production runtime. Scoped MCP
  tool calls inject that namespace and reject cross-namespace targets; mutation
  gateway rejects out-of-scope namespaces.

## Completed or verified work
- R02: PR #13
- R03: namespace scope module, MCP tool wrapping, ExecutionContext fail-closed,
  mutation gateway enforcement, focused tests.

## Active problem
R03 still needs commit/PR. Run-manifest namespace visibility waits on A01 merge.
RBAC-at-edge remains an edge-server concern beyond application enforcement.

## Relevant files
- `sre_agent/namespace_scope.py`, `sre_agent/mcp_tool_wrapper.py`,
  `sre_agent/multi_agent_langgraph.py`, `sre_agent/execution_context.py`,
  `sre_agent/mutation_gateway.py`, `tests/test_namespace_scope.py`

## Verification commands and latest results
- `pytest tests/test_namespace_scope.py tests/test_execution_context.py
  tests/test_mutation_gateway.py`: 21 passed

## Known blockers or risks
- Query rewriting covers common PromQL/LogQL selector shapes, not every DSL.
- Multi-namespace allowlists are supported, but production clusters should set
  one primary `cluster.namespace`.

## Next bounded task
Commit/push R03 PR against master, then continue with R08 fail-closed concurrency
admission (depends on R02) or R04 per-cluster model settings.
