# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Five-phase production upgrade track (Jira, observability, memory, multi-tenancy, benchmark) vs. HolmesGPT-class competitors. PR #44 (Temporal sandbox verification) and PR #45 (Slack conversational memory) are merged into `master`. PR #46 (Jira ticketing, phase 1) and PR #47 (Langfuse observability, phase 2) are open, behind `master`, and being rebased/merged next, in that order. Phases 3-5 (memory sophistication, multi-tenant secure access, AIOpsLab benchmark) are not yet started — see the active plan file for full detail on all five phases.

Temporal sandbox (PR #44) is a **log-based recovery oracle only**: replay the log evidence that proved an incident was broken, apply the proposed patch inside an isolated K8s Job, re-run, and diff logs to verdict RESOLVED/REGRESSED/INCONCLUSIVE. Not a general-purpose code interpreter or test runner.

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
PR #46 and PR #47 both went `CONFLICTING`/behind `master` once PR #44/#45 merged (same pattern PR #45 hit against PR #44: `benchmarks/release/candidate/bundle.json`'s `source_digest` is full-tree hashed, not diff-based, so any branch behind `master` needs a fresh merge + digest recompute before its release-evaluation gate will pass). Both PRs' CI also only shows 17 checks instead of 18 — harmless: PR #44 added a new `Edge MCP images (sandbox_real)` matrix job, and #46/#47 branched before that landed, so their copy of `.github/workflows/ci.yml` predates it. Resolves itself once each branch merges `master` in.

## Relevant files
- `sre_agent/sandbox_workflow.py`, `sre_agent/sandbox_gateway.py`, `sre_agent/temporal_client.py`, `sre_agent/sandbox_worker.py`
- `sre_agent/graph_builder.py` (`_act_gate_node` trigger), `sre_agent/executor.py` (`GITHUB_EXEC_TOOL_MAP`, sandbox tool map), `sre_agent/resolution_report.py` (`code_fix` field)
- `edge_mcp_servers/mcp_servers/sandbox_real/`
- `deploy/helm/sentinel/templates/rbac.yaml`, `temporal-worker.yaml`, `datastores.yaml`; `deploy/k8s/rbac.yaml`
- `tests/test_sandbox_workflow.py`, `tests/test_sandbox_gateway.py`, `tests/test_sandbox_temporal_workflow.py`, `tests/test_rbac_scope.py`
- `.github/workflows/ci.yml` (backend-tests: `--extra temporal`)
- `benchmarks/release_gate.py`, `benchmarks/release/v1/policy.json`, `benchmarks/release/candidate/bundle.json` (release-evidence gate; regenerate `candidate.source_digest` via `uv run python benchmarks/release_gate.py digest --policy benchmarks/release/v1/policy.json --repo-root .` after any merge from `master`)
- `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md` (full 5-phase plan: Jira / Langfuse / memory / multi-tenancy / AIOpsLab benchmark)

## Verification commands and latest results
- `uv run pytest -q` -> 693 passed, 2 skipped on `master` post PR #44+#45.
- `uv run python scripts/check_module_reachability.py` -> 71 reachable, 6 experimental.
- `bash scripts/check_helm_production.sh` / `bash scripts/check_kustomize.sh` -> pass.
- `gh pr checks 44` / `gh pr checks 45` -> 18/18 green before merge; both `MERGED`.

## Known blockers or risks
None external. PR #46 (Jira) needs real per-cluster Jira credentials from the user for full end-to-end validation eventually, but code/tests are self-contained and don't block merging. PR #47 (Langfuse) is fully self-hosted, no external account needed.

## Next bounded task
Merge `master` into `feature/jira-ticketing-integration` (PR #46), resolve conflicts (expect `bundle.json` digest, possibly `PROJECT_STATE.md`), recompute release digest, verify CI green, merge. Then repeat for `feature/langfuse-observability` (PR #47), merging the now-updated `master` (including #46) in first.
