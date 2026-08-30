# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Executing the 5-phase upgrade plan (Jira, observability, memory, multi-tenancy,
real benchmark) from `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md`,
one PR per phase, no cross-phase file overlap. PR #44 (Temporal sandbox
verification), PR #45 (Slack conversational memory), PR #46 (Jira ticketing,
Phase 1), and PR #47 (Langfuse observability, Phase 2) are all merged into
`master`. Phase 3 (memory sophistication) is code-complete and committed on
branch `feature/memory-sophistication`, pending push/PR/CI/merge. Phases 4-5
(multi-tenant secure access, AIOpsLab benchmark) are scoped in the plan file
but not started.

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
- **Observability (Phase 2, PR #47):** `sre_agent/tracing.py::langfuse_enabled()` is **opt-out** (`LANGFUSE_TRACING` defaults truthy). Self-hosted Langfuse (web/worker/clickhouse/minio, reusing the platform's own Postgres as a second logical DB `langfuse` and Redis DB index `/2`) wired in both `platform/docker-compose.yaml` and the Helm chart (`templates/langfuse.yaml`, gated by `.Values.langfuse.deploy`, default `true`). Headless `LANGFUSE_INIT_*` bootstrap auto-provisions org/project/API-keypair/admin-user so tracing works after a fresh boot with zero manual UI steps.
- **Incident memory (Phase 3, `feature/memory-sophistication`):** `sre_agent/memory_store.py` stores each incident as three separately-embedded named Qdrant vectors (`symptoms`/`root_cause`/`resolution`) in collection `sre_incidents_v2`, tenant-scoped by `organization_id`/`cluster_id` payload fields + Qdrant `Filter` on every search, recency-decayed ranking (`SENTINEL_MEMORY_RECENCY_HALF_LIFE_DAYS`, default 30d half-life), and cross-incident back-links computed at store time via a `root_cause`-similarity lookup. Point IDs are `uuid.uuid5(NAMESPACE, incident_id)` (deterministic across processes), not Python's `hash()` (see `DECISIONS.md`). Embedding bootstrap is unified in `sre_agent/embedding.py` — the process-wide `fastembed` singleton used by `memory_store.py` (`skill_store.py` has no embedding code to unify yet; `edge_mcp_servers/.../runbooks_local/server.py` intentionally keeps its own, since it's a separate customer-deployed container that never imports `sre_agent`).
- **Release Evaluation Contract:** `benchmarks/release_gate.py` gates prompt/model/tool changes against content-addressed evidence bundles; `candidate.source_digest` is a full-tree hash of files matching `protected_path_rules`, not diff-based — any branch behind `master` needs a fresh merge + digest recompute (`uv run python benchmarks/release_gate.py digest ...`) before the gate passes. Zero protected-path changes → `NOT_REQUIRED` (passes regardless of bundle content).
- **Module Reachability Governance:** `scripts/check_module_reachability.py`; standalone `python -m` workers with no in-process caller are declared directly as `ENTRY_FILES` roots.
- **Alembic:** single linear head; PR #46's `db94419c24dc` (Jira columns) now chains after PR #45's `f6a7b8c9d0e1` (Slack columns) — both originally branched from the same parent and required a manual `down_revision` re-point during merge.

## Completed or verified work
- All 41 backlog packages + PR #34 (LLM provider restriction), PR #42, PR #43 merged.
- PR #44: Temporal sandbox verification workflow. PR #45: Slack conversational memory. PR #46: Jira ticketing (Phase 1). PR #47: Langfuse observability (Phase 2). All merged to `master`.
- Phase 3 (memory sophistication), all three parts done on `feature/memory-sophistication`:
  1. **Tenant filter:** `store_incident()`/`search_similar_incidents()` gained `organization_id`/`cluster_id` kwargs; search builds a Qdrant `Filter`. All 4 real call sites (`agent_runtime.py`, `supervisor.py` x2, `graph_builder.py`) updated. Confirmed the `store_incident_memory`/`recall_similar_incidents` MCP-tool lookups referenced in `supervisor.py`/`graph_builder.py` are speculative (no MCP server in-repo registers those names) — the direct `MemoryStore` path is the only one that executes today.
  2. **Structured payload:** `store_incident()` now takes `symptoms`/`root_cause`/`resolution` instead of one flat `incident_text`, embedded as three named Qdrant vectors in collection `sre_incidents_v2` (renamed from `sre_incidents` — incompatible schema change, no production data to migrate). `search_similar_incidents()` queries all three fields, dedups by `incident_id` keeping the best raw score, then re-ranks by recency-decayed score. Both store call sites (`agent_runtime.py`, `supervisor.py`) updated to pass the three fields from data they already had in scope (`alert_name`/`reflector_analysis.hypothesis`/`final_response` or `plan_hypothesis`).
  3. **Cross-incident back-links:** at store time, a `root_cause`-similarity query finds related past incidents (tenant-scoped, `RELATED_SCORE_THRESHOLD=0.5`, `related_limit=3` default); the new incident's payload gets `related_incident_ids`, and each related incident's own payload is updated (`client.retrieve` + `set_payload`) to back-link the new incident. Surfaced in `format_similar_incidents_for_prompt()`.
  4. **Embedding unification:** new `sre_agent/embedding.py` — shared lazy `fastembed.TextEmbedding` singleton (`SENTINEL_EMBEDDING_MODEL` env override, default `BAAI/bge-small-en-v1.5`); `memory_store.py` now uses it instead of its own instance.
  - `tests/test_memory_store.py` (new, 13 tests): tenant payload/filter, no-mutation, related-incident computation + back-linking, `related_limit=0` skip, per-field query dedup, recency-decay reordering, related-ids surfaced in results/prompt, point-ID determinism, decay bounds.
  - `docs/ai/DECISIONS.md` gained "Incident memory uses named vectors and deterministic point IDs" entry.
  - `.env.example` gained a Qdrant/memory section documenting `QDRANT_URL` (pre-existing, previously undocumented) and the two new optional tuning vars.

## Active problem
None. Phase 3 is committed on `feature/memory-sophistication`, not yet pushed. Next action is push + open PR + verify CI green + merge (same flow as PR #44-#47).

## Relevant files
- `sre_agent/memory_store.py` (Phase 3 — structured payload, tenant filter, recency decay, back-links)
- `sre_agent/embedding.py` (new — shared embedding singleton)
- `sre_agent/agent_runtime.py`, `sre_agent/supervisor.py`, `sre_agent/graph_builder.py` (memory_store call sites, tenant-scoped + structured fields)
- `tests/test_memory_store.py` (new — full `MemoryStore` unit coverage, no live Qdrant needed)
- `docs/ai/DECISIONS.md`, `.env.example` (Phase 3 documentation)
- `sre_agent/tracing.py`, `platform/docker-compose.yaml`, `deploy/helm/sentinel/templates/langfuse.yaml` (Phase 2, merged)
- `sre_agent/integrations/jira.py`, `sre_agent/api/v1/tickets.py` (Phase 1, merged)
- `benchmarks/release_gate.py`, `benchmarks/release/v1/policy.json`, `benchmarks/release/candidate/bundle.json`
- `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md` (authoritative phase plan)

## Verification commands and latest results
- `uv run pytest -q` → 716 passed, 2 skipped, on `feature/memory-sophistication` (baseline 702 on `master` + 13 new `test_memory_store.py` + 1 more collected).
- `uv run pytest -q tests/test_memory_store.py` → 13 passed.
- `uv run ruff check sre_agent/memory_store.py sre_agent/embedding.py` → all checks passed (pre-existing lint debt elsewhere in `supervisor.py`/`agent_runtime.py`/`graph_builder.py` untouched by this diff).
- `uv run python scripts/check_module_reachability.py` → `Reachability OK: 72 reachable, 6 experimental.`
- `gh pr checks 44` / `45` / `46` / `47` → all green before merge; all `MERGED`.

## Known blockers or risks
- None technical. Phase 4 (multi-tenant secure access) will need a real GitHub App + Slack app registration from the user for end-to-end OAuth validation — not needed until that phase.

## Next bounded task
Push `feature/memory-sophistication`, open PR, verify CI green, merge (mirroring
PR #44-#47). Then start Phase 4 (multi-tenant secure access) in a **fresh
conversation**: new `sre_agent/multitenant/` package (`github_app.py`,
`slack_oauth.py`, `relay_auth.py`) replacing the single shared
`MCP_SERVICE_TOKEN` with per-tenant issued credentials, extending
`edge_mcp_servers/*` to accept per-installation credentials from `Cluster`'s
already-encrypted columns instead of only `KUBECONFIG`; builds on
`mutation_gateway.py::_verify_scope()`/`namespace_scope.py`. Needs a real
GitHub App + Slack app registration from the user for end-to-end OAuth
validation. Read this file plus the plan file's "Phases 3-5" section before
starting.
