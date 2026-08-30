# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Executing the 5-phase upgrade plan (Jira, observability, memory, multi-tenancy,
real benchmark) from `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md`,
one PR per phase, no cross-phase file overlap. Phase 1 (Jira ticketing) merged
as PR #46. Phase 2 (self-hosted Langfuse observability, wired on by default)
is complete and open as **PR #47** (`feature/langfuse-observability`,
commit `a91ca2a`). Phases 3–5 (memory sophistication, multi-tenant secure
access, AIOpsLab benchmark) are scoped in the plan file but not started.

## Current architecture and invariants
- **Strict LLM Provider Guard:** `sre_agent.provider_config.SUPPORTED_PROVIDERS` / `sre_agent.cluster_context.SUPPORTED_LLM_PROVIDERS` restrict to `anthropic` and `gemini`; `validate_startup_config` checks credentials at boot.
- **Single Canonical Runner:** all production callers invoke `sre_agent.incident_runner.run_incident_investigation`; `agent_runtime_tasks.py` is a quarantined forwarding shim.
- **Durable Job Worker Pipeline:** Postgres lease-backed jobs with heartbeats, retries, dead-letter queueing (`sre_agent/job_worker.py`).
- **Unified ORM & Migration Linearity:** all models inherit `backend.models.Base`; Alembic single-head chain ending `2253eabf13e3`.
- **Distributed Live Events:** `sre_agent.live_events` via Redis pub/sub with in-memory fallback.
- **Evidence-Based Severity & Fail-Closed Logic:** `sre_agent.severity_engine` never fabricates calm values from missing telemetry.
- **Mutation Gateway & Safety:** `sre_agent.mutation_gateway` enforces namespace/tenant scope, idempotency locks, approval interrupts, audit logs.
- **Verified Learning:** `sre_agent.act_phase` requires verified resolution before `skill_store` promotion.
- **Release Evaluation Contract:** `benchmarks/release_gate.py` gates prompt/model/tool changes against content-addressed evidence bundles.
- **Module Reachability Governance:** `scripts/check_module_reachability.py` blocks unmanaged top-level `sre_agent/` modules.
- **Observability (new, Phase 2):** `sre_agent/tracing.py::langfuse_enabled()` is now **opt-out** (`LANGFUSE_TRACING` defaults truthy), not opt-in. Self-hosted Langfuse (web/worker/clickhouse/minio, reusing the platform's own Postgres as a second logical DB `langfuse` and Redis DB index `/2`) is wired in both `platform/docker-compose.yaml` and the Helm chart (`templates/langfuse.yaml`, gated by `.Values.langfuse.deploy`, default `true`). Headless `LANGFUSE_INIT_*` bootstrap auto-provisions org/project/API-keypair/admin-user so tracing works after a fresh boot with zero manual UI steps. `secret.yaml` fails Helm install fast if Langfuse secrets are left at placeholder values while `tracing.enabled`/`langfuse.deploy` are true.

## Completed or verified work
- All 41 backlog packages + PR #34 (LLM provider restriction) merged to `master`.
- PR #42 (post-integration operational fixes) and PR #43 (tenant-scoped MCP audit restore) merged.
- PR #46: Jira ticketing (Phase 1), per-Cluster DB-column credentials (deviated from global env vars for real multi-tenant SaaS correctness).
- PR #47: Langfuse observability (Phase 2) — `tracing.py` opt-out flip, `langfuse` added as a hard dependency, full docker-compose stack, full Helm chart addition (`langfuse.yaml`, `_helpers.tpl` helpers `sentinel.langfuseHost`/`langfuseRedisUrl`/`langfuseDbInitContainer`, `values.yaml`/`values-production.yaml`, `secret.yaml` fail-fast checks, `configmap.yaml`, `networkpolicy.yaml` grants), `.env.example` docs, `test_tracing.py` default flipped.

## Active problem
None. PR #47 is open, verified, and ready for review/merge.

## Relevant files
- `sre_agent/tracing.py` (Langfuse callback wiring, opt-out default)
- `platform/docker-compose.yaml` (local self-hosted Langfuse stack)
- `deploy/helm/sentinel/templates/langfuse.yaml`, `_helpers.tpl`, `secret.yaml`, `configmap.yaml`, `networkpolicy.yaml`, `values.yaml`, `values-production.yaml`
- `sre_agent/provider_config.py`, `sre_agent/incident_runner.py`, `sre_agent/job_worker.py`, `sre_agent/mutation_gateway.py`, `sre_agent/severity_engine.py`, `sre_agent/act_phase.py` (unchanged core invariants, listed for orientation)
- `sre_agent/memory_store.py` (Phase 3 target — `search_similar_incidents()` lacks `org_id`/`cluster_id` filtering despite `cluster_id` already in the Qdrant payload)
- `/Users/jayan/.claude/plans/groovy-toasting-cupcake.md` (authoritative phase plan)

## Verification commands and latest results
- `PYTHONPATH=. uv run pytest -q` → 675 passed.
- `docker compose -f platform/docker-compose.yaml config --quiet` → exit 0.
- `helm lint deploy/helm/sentinel` (with required `--set secrets.*`) → 0 failed.
- `helm template` → 51 non-duplicated manifests, verified in 3 configs (default, `values-production.yaml` overlay, `tracing.enabled=false`+`langfuse.deploy=false` opt-out).

## Known blockers or risks
- None technical. PR #47 merge is a shared-state action pending user decision.
- Phase 4 (multi-tenant secure access) will need a real GitHub App + Slack app registration from the user for end-to-end OAuth validation — not needed until that phase.

## Next bounded task
Start Phase 3 (memory sophistication) in a **fresh conversation**: fix
`memory_store.py::search_similar_incidents()`'s missing `org_id`/`cluster_id`
filter (cross-tenant leak risk), split the flat payload into structured
separately-embedded fields (root cause / resolution / symptoms) with recency
decay and cross-incident back-links, and unify the three disconnected
embedding pipelines (`memory_store.py`, `skill_store.py`, `runbooks_local/server.py`)
behind one shared `sre_agent/embedding.py`. Fully verifiable locally against
the Qdrant in `platform/docker-compose.yaml` — no external account needed.
Read this file plus the plan file's "Phases 3–5" section before starting.
