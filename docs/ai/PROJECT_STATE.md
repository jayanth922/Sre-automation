# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Executing the 5-phase upgrade plan (Jira, observability, memory, multi-tenancy,
real benchmark) from `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md`,
one PR per phase, no cross-phase file overlap. PR #44 (Temporal sandbox
verification), PR #45 (Slack conversational memory), PR #46 (Jira ticketing,
Phase 1), PR #47 (Langfuse observability, Phase 2), PR #48 (memory
sophistication, Phase 3), and PR #49 (multi-tenant secure access, Phase 4)
are all merged into `master`. Phase 5 (AIOpsLab benchmark) is the next
milestone — scoped in the plan file but not started.

Real end-to-end OAuth validation for Phase 4 (an actual GitHub App + Slack
app registered by the user) is intentionally deferred — user is configuring
those later; token-scope logic and all regression tests are verified locally
without them.

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
- **Incident memory (Phase 3, PR #48, merged):** `sre_agent/memory_store.py` stores each incident as three separately-embedded named Qdrant vectors (`symptoms`/`root_cause`/`resolution`) in collection `sre_incidents_v2`, tenant-scoped by `organization_id`/`cluster_id` payload fields + Qdrant `Filter` on every search, recency-decayed ranking (`SENTINEL_MEMORY_RECENCY_HALF_LIFE_DAYS`, default 30d half-life), and cross-incident back-links computed at store time via a `root_cause`-similarity lookup. Point IDs are `uuid.uuid5(NAMESPACE, incident_id)` (deterministic across processes), not Python's `hash()` (see `DECISIONS.md`). Embedding bootstrap is unified in `sre_agent/embedding.py` — the process-wide `fastembed` singleton used by `memory_store.py` (`skill_store.py` has no embedding code to unify yet; `edge_mcp_servers/.../runbooks_local/server.py` intentionally keeps its own, since it's a separate customer-deployed container that never imports `sre_agent`).
- **Release Evaluation Contract:** `benchmarks/release_gate.py` gates prompt/model/tool changes against content-addressed evidence bundles; `candidate.source_digest` is a full-tree hash of files matching `protected_path_rules`, not diff-based — any branch behind `master` needs a fresh merge + digest recompute (`uv run python benchmarks/release_gate.py digest ...`) before the gate passes. Zero protected-path changes → `NOT_REQUIRED` (passes regardless of bundle content).
- **Module Reachability Governance:** `scripts/check_module_reachability.py`; standalone `python -m` workers with no in-process caller are declared directly as `ENTRY_FILES` roots.
- **Alembic:** single linear head; PR #46's `db94419c24dc` (Jira columns) now chains after PR #45's `f6a7b8c9d0e1` (Slack columns) — both originally branched from the same parent and required a manual `down_revision` re-point during merge. Phase 4's `a3f7c1d9b2e4` (GitHub App installation ID, Slack OAuth columns) chains after `db94419c24dc` and is the current sole head on `master`.
- **Multi-tenant secure access (Phase 4, PR #49, merged):** new `sre_agent/multitenant/` package — `github_app.py` mints short-lived (~1h) GitHub App installation tokens (RS256 JWT via `python-jose`, already a dependency) when `Cluster.github_app_installation_id` is set, falling back non-fatally to the stored `github_token` PAT on any failure; `slack_oauth.py` implements Slack's "Add to Slack" OAuth v2 flow, storing a per-`Organization` bot token/team ID (`slack_bot_token`/`slack_team_id`, new encrypted/plain columns) instead of only the global `SLACK_BOT_TOKEN` env var; `relay_auth.py::build_relay_headers()` relays one cluster's resolved GitHub/K8s credentials as `X-Sentinel-Relay-*` headers alongside the existing tenant-identity headers on each investigation's fresh MCP connection (`build_mcp_server_config`/`create_mcp_client` are now `async def` for this). Edge side: `edge_mcp_servers/relay_credentials.py` (new, dependency-free — never imports `sre_agent`) captures those headers into a `contextvars.ContextVar` from the existing bearer-auth ASGI middleware (`mcp_auth.py`); `github_real/server.py::_active_repo()` and `k8s_real/server.py::_relay_api_client()` are the only two choke points that read them back, each a small bounded cache (max 8 entries), falling back to the static single-tenant `GITHUB_TOKEN`/`KUBECONFIG` env-var path when nothing was relayed. New API routes (`sre_agent/api/v1/multitenant.py`) let an authenticated user start each flow (`GET /api/v1/organizations/slack/install-url`, `GET /api/v1/clusters/{id}/github-app/install-url`) and an unauthenticated callback finish it (`GET /api/v1/organizations/slack/callback`, `GET /api/v1/clusters/github-app/callback`) — CSRF/identity state is a short-lived (10 min) signed JWT via `backend.auth.create_access_token`/`decode_access_token` (a `purpose` claim scopes it), not server-side session storage, so it works across multiple API workers. See `docs/ai/DECISIONS.md`'s "Per-cluster credentials relay over the MCP transport" entry for the full rationale.

## Completed or verified work
- All 41 backlog packages + PR #34 (LLM provider restriction), PR #42, PR #43 merged.
- PR #44: Temporal sandbox verification. PR #45: Slack conversational memory. PR #46: Jira ticketing (Phase 1). PR #47: Langfuse observability (Phase 2). PR #48: Memory sophistication (Phase 3, tenant-filtered/structured/back-linked Qdrant memory — see `sre_agent/memory_store.py` and `docs/ai/DECISIONS.md`). PR #49: Multi-tenant secure access (Phase 4 — see "Current architecture" bullet above). All merged to `master`.

## Active problem
None. Phase 4 is merged and verified. Real GitHub App/Slack app registration
and end-to-end OAuth validation are deferred until the user configures those
externally; the user has said they'll report back if issues surface while
running the application.

## Relevant files
- `sre_agent/multitenant/{github_app,slack_oauth,relay_auth}.py` (Phase 4, merged)
- `sre_agent/api/v1/multitenant.py` (Phase 4, merged — Slack/GitHub App install+callback routes)
- `edge_mcp_servers/relay_credentials.py` (Phase 4, merged — dependency-free edge-side contextvar capture)
- `edge_mcp_servers/mcp_auth.py`, `edge_mcp_servers/mcp_servers/{github_real,k8s_real}/server.py`, all 8 `edge_mcp_servers/mcp_servers/*/Dockerfile` (Phase 4 — relay wiring)
- `backend/models.py`, `backend/schemas.py`, `backend/crud.py`, `backend/alembic/versions/a3f7c1d9b2e4_*.py` (Phase 4 — `github_app_installation_id`, `slack_bot_token`, `slack_team_id`)
- `sre_agent/execution_context.py`, `sre_agent/multi_agent_langgraph.py` (Phase 4 — credentials folded into `ExecutionContext`, MCP client builder now `async`)
- `sre_agent/integrations/slack_bot.py` (Phase 4 — `build_slack_app(organization=...)` resolves the OAuth-installed token when given one)
- `tests/test_multitenant_{github_app,slack_oauth,relay_auth}.py` (Phase 4, merged); `tests/test_mcp_auth.py`, `tests/test_canonical_models.py` (Phase 4 — updated for the new relay import + Alembic head)
- `docs/ai/DECISIONS.md` "Per-cluster credentials relay over the MCP transport" entry, `.env.example` (Phase 4 documentation)
- `sre_agent/memory_store.py`, `sre_agent/embedding.py` (Phase 3, merged)
- `sre_agent/tracing.py`, `platform/docker-compose.yaml`, `deploy/helm/sentinel/templates/langfuse.yaml` (Phase 2, merged)
- `sre_agent/integrations/jira.py`, `sre_agent/api/v1/tickets.py` (Phase 1, merged — structural template Phase 4's API routes followed)
- `benchmarks/release_gate.py`, `benchmarks/release/v1/policy.json`, `benchmarks/release/candidate/bundle.json`
- `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md` (authoritative phase plan)

## Verification commands and latest results
- `uv run pytest -q` → 750 passed, 2 skipped, on PR #49 before merge (34 new Phase 4 tests; one pre-existing hardcoded-head test and one stale source-string assertion in `test_mcp_auth.py` updated to match the new code, not skipped).
- `uv run ruff check <all Phase 4 new/changed files>` → zero errors introduced; pre-existing lint debt in `agent_runtime.py`/`multi_agent_langgraph.py`/`models.py`/etc. confirmed unchanged by diffing against `master` before touching those files.
- `uv run python scripts/check_module_reachability.py` → `Reachability OK: 72 reachable, 6 experimental.`
- Alembic: `a3f7c1d9b2e4` confirmed as sole head on `master`, chained after `db94419c24dc`.
- PR #49 CI: all checks passed, including `benchmarks/release_gate.py`'s "Release evaluation contract" — required a fixture `change_class`/`source_digest` regeneration (`benchmarks/release/candidate/bundle.json`) since this PR's edge-server changes fall under the "tool" protected-path category; same fix pattern as prior commits `e63ed84`/`8fdea10`/`82659a7`.

## Known blockers or risks
- Real end-to-end OAuth validation (an actual GitHub App + Slack app
  registered by the user) has not been done — token-scope logic and all
  regression tests are verified locally without them. User is configuring
  the GitHub App and Slack app separately and will report back if issues
  come up running the application.
- The Slack OAuth callback and GitHub App callback routes return bare
  JSON (no frontend redirect UX yet — no `FRONTEND_URL` convention existed
  in this repo to redirect to; deferred as a separate frontend task).

## Next bounded task
Phase 5 (AIOpsLab benchmark) is the next milestone; start it in a **fresh
conversation**, reading this file plus the plan file's "Phases 3-5" section
first. No open work on Phase 4 unless the user reports an issue while
configuring/running GitHub App or Slack integration.
