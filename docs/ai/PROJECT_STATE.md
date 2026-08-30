# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Temporal-orchestrated sandbox for verifying AI-generated code fixes, implemented on `master` (built on top of merged PRs #34/#42/#43). Sandbox is a **log-based recovery oracle only**: replay the log evidence that proved an incident was broken, apply the proposed patch inside an isolated K8s Job, re-run, and diff logs to verdict RESOLVED/REGRESSED/INCONCLUSIVE. Not a general-purpose code interpreter or test runner.

## Current architecture and invariants
- **Strict LLM Provider Guard:** `sre_agent.provider_config.SUPPORTED_PROVIDERS` restricts model operations to `anthropic` and `gemini`.
- **Single Canonical Runner:** production callers invoke the LangGraph pipeline via `sre_agent.incident_runner.run_incident_investigation`.
- **Durable Job Worker Pipeline:** `sre_agent/job_worker.py` — PostgreSQL lease-backed durable jobs.
- **Mutation Gateway & Safety:** cluster writes pass `sre_agent.mutation_gateway`; sandbox K8s Job lifecycle passes the analogous `sre_agent.sandbox_gateway.authorize_and_provision_sandbox` (same tenant/namespace/idempotency/audit checks, registered in `namespace_scope._NAMESPACE_ARG_TOOLS`).
- **Code-fix verification workflow:** `sre_agent/sandbox_workflow.py::CodeFixVerificationWorkflow` (Temporal) — 6 activities (baseline → apply_patch → candidate → verify_recovery → emit_verdict → cleanup), each with a bounded `RetryPolicy(maximum_attempts=3)`; `cleanup_activity` runs in `try/finally` so K8s Job teardown is guaranteed even on activity failure. `diff_logs()` is the pure oracle function. Verdict lands via `incident_timeline.emit_timeline_event(event_type="act", ...)` and `resolution_report.code_fix`.
- **Trigger point:** `sre_agent/graph_builder.py::_act_gate_node` — after ACT phase, scans `action_reports` for a `GITHUB_EXEC_TOOL_MAP` action with sandbox parameters; fires `temporal_client.start_workflow(...)` fire-and-forget (non-blocking on OODA loop), sets `code_fix.status = "VERIFYING"`, or `"INCONCLUSIVE"` gracefully if Temporal is disabled/misconfigured. Never raises into ACT report generation.
- **Temporal deploy:** self-hosted via Helm (`temporal.deploy` toggle in `values.yaml`), Postgres-backed (`temporal`/`temporal_visibility` DBs in the existing Postgres instance). Worker (`deploy/helm/sentinel/templates/temporal-worker.yaml`) reuses the `sentinel/api` image, runs `python -m sre_agent.sandbox_worker` — no new Dockerfile/CI image job.
- **Sandbox RBAC:** dedicated `sentinel-sandbox` namespace (always rendered regardless of `rbac.namespaced`/`rbac.clusterWide`), least-privilege Role: `batch/jobs` (create/get/list/watch/delete) + `pods/log` (get/list/watch) only. The actuator's separate pods-only-delete invariant (`tests/test_rbac_scope.py::_assert_delete_is_pods_only`) is scoped to skip this always-rendered sandbox block (it's a distinct ServiceAccount/namespace/concern).
- **Module Reachability Governance:** `scripts/check_module_reachability.py`; standalone `python -m` workers with no in-process caller (`job_worker.py`, `sandbox_worker.py`) are declared directly as `ENTRY_FILES` roots.
- **Dependency layering:** `temporalio` is a `temporal` optional extra in `pyproject.toml` (`pip install sre-agent[temporal]`); `kubernetes` client lives only in `edge_mcp_servers/mcp_servers/sandbox_real/requirements.txt`. Neither is in the base lock — CI's `backend-tests` job now runs `uv sync --frozen --extra dev --extra temporal` so the Temporal-dependent tests actually execute (previously would have silently skipped via `pytest.importorskip`).

## Completed or verified work
- All 10 plan components implemented: `sandbox_real` edge MCP server, sandbox RBAC (plain manifest + Helm), `sandbox_gateway.py`, `executor.py` sandbox tool map, `temporal_client.py`, `sandbox_workflow.py`, `graph_builder.py` trigger wiring, self-hosted Temporal Helm deploy + `sandbox_worker.py`, `temporal` optional dependency extra, full test coverage (unit + gateway + Temporal `WorkflowEnvironment` workflow tests).
- Fixed a genuine production bug found via testing: workflow activities had no bounded retry policy, so Temporal's unbounded default retries would stall guaranteed cleanup indefinitely on a persistently-failing sandbox stage. Fixed with `RetryPolicy(maximum_attempts=3)` on all 6 activity calls.
- Fixed a test-invariant regression: `test_rbac_scope.py`'s pods-only-delete check was scanning the whole Helm RBAC template and choked on the new sandbox Role's legitimate `batch/jobs` delete verb; rescoped the check to exclude the always-rendered sandbox block.

## Active problem
None. All plan verification steps are green (see below).

## Relevant files
- `sre_agent/sandbox_workflow.py`, `sre_agent/sandbox_gateway.py`, `sre_agent/temporal_client.py`, `sre_agent/sandbox_worker.py`
- `sre_agent/graph_builder.py` (`_act_gate_node` trigger), `sre_agent/executor.py` (`GITHUB_EXEC_TOOL_MAP`, sandbox tool map), `sre_agent/resolution_report.py` (`code_fix` field)
- `edge_mcp_servers/mcp_servers/sandbox_real/`
- `deploy/helm/sentinel/templates/rbac.yaml`, `temporal-worker.yaml`, `datastores.yaml`; `deploy/k8s/rbac.yaml`
- `tests/test_sandbox_workflow.py`, `tests/test_sandbox_gateway.py`, `tests/test_sandbox_temporal_workflow.py`, `tests/test_rbac_scope.py`
- `.github/workflows/ci.yml` (backend-tests: `--extra temporal`)

## Verification commands and latest results
- `uv run pytest -q` -> 693 passed (0 failures; up from 673 pre-milestone).
- `uv run python scripts/check_module_reachability.py` -> 71 reachable, 6 experimental.
- `bash scripts/check_helm_production.sh` -> Helm production capability checks passed.
- `helm template --set temporal.deploy=true` (with dummy secrets) -> renders cleanly; verified `sentinel-temporal-worker` Deployment (image `sentinel/api:latest`, command `sre_agent.sandbox_worker`), `temporal` server, `mcp-sandbox` edge server, and `sentinel-sandbox` namespace/Role/RoleBinding all present.

## Known blockers or risks
None outstanding. A separate, independent milestone (real Slack conversational integration for on-call, PR TBD) lands in parallel — it does not touch any file in this milestone's diff.

## Next bounded task
Merge this PR. A follow-up doc pass should reconcile this file with the parallel Slack-integration PR once both have landed (this file currently reflects only the Temporal milestone).
