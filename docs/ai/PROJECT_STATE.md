# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Executing the 5-phase upgrade plan (Jira, observability, memory, multi-tenancy,
real benchmark) from `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md`,
one PR per phase, no cross-phase file overlap. PR #44 (Temporal sandbox
verification), PR #45 (Slack conversational memory), and PR #46 (Jira
ticketing, Phase 1) are merged into `master`. Phase 2 (self-hosted Langfuse
observability, wired on by default) is complete and open as **PR #47**
(`feature/langfuse-observability`) — currently being rebased onto `master`
and merged. Phases 3-5 (memory sophistication, multi-tenant secure access,
AIOpsLab benchmark) are scoped in the plan file but not started.

Temporal sandbox (PR #44) is a **log-based recovery oracle only**: replay the
log evidence that proved an incident was broken, apply the proposed patch
inside an isolated K8s Job, re-run, and diff logs to verdict
RESOLVED/REGRESSED/INCONCLUSIVE. Not a general-purpose code interpreter or
test runner.

## Current architecture and invariants
- **Strict LLM Provider Guard:** `sre_agent.provider_config.SUPPORTED_PROVIDERS` restricts model operations to `anthropic` and `gemini`.
- **Single Canonical Runner:** production callers invoke the LangGraph pipeline via `sre_agent.incident_runner.run_incident_investigation`.
- **Durable Job Worker Pipeline:** `sre_agent/job_worker.py` — PostgreSQL lease-backed durable jobs.
- **Mutation Gateway & Safety:** cluster writes pass `sre_agent.mutation_gateway`; sandbox K8s Job lifecycle passes the analogous `sre_agent.sandbox_gateway.authorize_and_provision_sandbox` (same tenant/namespace/idempotency/audit checks, registered in `namespace_scope._NAMESPACE_ARG_TOOLS`).
- **Code-fix verification workflow:** `sre_agent/sandbox_workflow.py::CodeFixVerificationWorkflow` (Temporal) — 6 activities (baseline → apply_patch → candidate → verify_recovery → emit_verdict → cleanup), each with a bounded `RetryPolicy(maximum_attempts=3)`; `cleanup_activity` runs in `try/finally`. `diff_logs()` is the pure oracle function.
- **Sandbox RBAC:** dedicated `sentinel-sandbox` namespace, least-privilege Role: `batch/jobs` (create/get/list/watch/delete) + `pods/log` (get/list/watch) only.
- **Jira ticketing (Phase 1, PR #46):** per-Cluster DB-column credentials (`jira_url`/`jira_email`/`jira_api_token`/`jira_project_key`), not global env vars — deviated from the original plan sketch for real multi-tenant SaaS correctness (each customer has their own Jira site). `incidents.jira_issue_key` links incident to ticket.
- **Observability (Phase 2, PR #47, in flight):** `sre_agent/tracing.py::langfuse_enabled()` flips **opt-out** (`LANGFUSE_TRACING` defaults truthy), not opt-in. Self-hosted Langfuse (web/worker/clickhouse/minio, reusing the platform's own Postgres as a second logical DB `langfuse` and Redis DB index `/2`) wired in both `platform/docker-compose.yaml` and the Helm chart (`templates/langfuse.yaml`, gated by `.Values.langfuse.deploy`, default `true`). Headless `LANGFUSE_INIT_*` bootstrap auto-provisions org/project/API-keypair/admin-user so tracing works after a fresh boot with zero manual UI steps.
- **Release Evaluation Contract:** `benchmarks/release_gate.py` gates prompt/model/tool changes against content-addressed evidence bundles; `candidate.source_digest` is a full-tree hash of files matching `protected_path_rules`, not diff-based — any branch behind `master` needs a fresh merge + digest recompute (`uv run python benchmarks/release_gate.py digest ...`) before the gate passes. Zero protected-path changes → `NOT_REQUIRED` (passes regardless of bundle content).
- **Module Reachability Governance:** `scripts/check_module_reachability.py`; standalone `python -m` workers with no in-process caller are declared directly as `ENTRY_FILES` roots.
- **Alembic:** single linear head; PR #46's `db94419c24dc` (Jira columns) now chains after PR #45's `f6a7b8c9d0e1` (Slack columns) — both originally branched from the same parent and required a manual `down_revision` re-point during merge.

## Completed or verified work
- All 41 backlog packages + PR #34 (LLM provider restriction), PR #42, PR #43 merged.
- PR #44: Temporal sandbox verification workflow. PR #45: Slack conversational memory. PR #46: Jira ticketing (Phase 1). All merged to `master`.
- PR #47: Langfuse observability (Phase 2) — `tracing.py` opt-out flip, `langfuse` added as a hard dependency, full docker-compose stack, full Helm chart addition (`langfuse.yaml`, `_helpers.tpl` helpers `sentinel.langfuseHost`/`langfuseRedisUrl`/`langfuseDbInitContainer`, `values.yaml`/`values-production.yaml`, `secret.yaml` fail-fast checks, `configmap.yaml`, `networkpolicy.yaml` grants), `.env.example` docs, `test_tracing.py` default flipped. Currently mid-merge onto `master` (conflicts in `_helpers.tpl`, `values.yaml` resolved by keeping both branches' additive blocks).

## Active problem
PR #47 was behind `master` (PR #44/#45/#46 landed after it branched) — merging `master` in now, resolving conflicts, and re-verifying before merge. Same CI-check-count quirk as #46 hit: this branch's `.github/workflows/ci.yml` predates the `Edge MCP images (sandbox_real)` matrix job added by PR #44, resolves once merged.

## Relevant files
- `sre_agent/tracing.py` (Langfuse callback wiring, opt-out default)
- `platform/docker-compose.yaml` (local self-hosted Langfuse stack)
- `deploy/helm/sentinel/templates/langfuse.yaml`, `_helpers.tpl`, `secret.yaml`, `configmap.yaml`, `networkpolicy.yaml`, `values.yaml`, `values-production.yaml`
- `sre_agent/integrations/jira.py`, `sre_agent/api/v1/tickets.py` (Phase 1)
- `sre_agent/sandbox_workflow.py`, `sre_agent/sandbox_gateway.py`, `sre_agent/temporal_client.py` (PR #44)
- `sre_agent/memory_store.py` (Phase 3 target — `search_similar_incidents()` lacks `org_id`/`cluster_id` filtering despite `cluster_id` already in the Qdrant payload)
- `benchmarks/release_gate.py`, `benchmarks/release/v1/policy.json`, `benchmarks/release/candidate/bundle.json`
- `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md` (authoritative phase plan)

## Verification commands and latest results
- `uv run pytest -q` → 702 passed, 2 skipped on `master` post PR #44/#45/#46.
- `docker compose -f platform/docker-compose.yaml config --quiet` → exit 0 (PR #47 branch, pre-merge).
- `helm lint deploy/helm/sentinel` (with required `--set secrets.*`) → 0 failed (PR #47 branch, pre-merge).
- `gh pr checks 44` / `45` / `46` → all green before merge; all `MERGED`.

## Known blockers or risks
- None technical. Phase 4 (multi-tenant secure access) will need a real GitHub App + Slack app registration from the user for end-to-end OAuth validation — not needed until that phase.

## Next bounded task
Finish merging `master` into `feature/langfuse-observability` (PR #47): re-run full test suite + `helm lint`/`docker compose config` post-merge, push, verify CI green (18/18), merge PR #47. Then start Phase 3 (memory sophistication) in a **fresh conversation**: fix `memory_store.py::search_similar_incidents()`'s missing `org_id`/`cluster_id` filter, split the flat payload into structured separately-embedded fields with recency decay and cross-incident back-links, unify the three disconnected embedding pipelines behind one shared `sre_agent/embedding.py`. Read this file plus the plan file's "Phases 3-5" section before starting.
