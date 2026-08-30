# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Post-integration regression fixes are implemented on `codex/post-integration-regression-fixes` from `master` (`d28a714`) after all 41 backlog packages and PR #34 were merged.

### Integrated Backlog Tracks & Platform Additions
- **Foundation (T01–T10):** Multi-agent LangGraph core, MCP adapters, state store, observability, and evaluation baseline.
- **Evaluation & Guardrails (A01–A10):** Run provenance (A01), recovery grading (A02), MTTR/diagnostics statistics (A03–A04), task-specific confidence calibration (A05), prompt-injection & adversarial safety (A06–A07), trace accounting (A08), release evaluation gates (A09), and verified-only learning (A10).
- **Robustness & Operations (R01–R11):** Mutation gateway & locks (R01), durable job leases (R02), namespace isolation (R03), per-cluster model routing (R04), canonical graph runner (R05), canonical audit log storage & retention (R06), truthful cluster heartbeat (R07), fail-closed admission concurrency (R08), distributed live event bus (R09), evidence-based severity engine (R10), and external incident/PR loops (R11).
- **Production Platform (P01–P11):** Provider config defaults (P01), typed settings (P02), websocket routing (P03), production Helm chart (P04), Terraform Helm module (P05), generic platform overlays (P06), ORM model consolidation (P07), CI quality gates (P08), integration test layers (P09), dead module reachability enforcement (P10), and truthful documentation & benchmark fixtures (P11).
- **Supported LLM Providers (PR #34):** Supported providers across the platform are restricted strictly to `anthropic` (default) and `gemini`. Legacy providers (`groq`, `ollama`, `nvidia`, `openai`, `openai_compatible`) fail closed with actionable migration guidance.

## Current architecture and invariants
- **Strict LLM Provider Guard:** `sre_agent.provider_config.SUPPORTED_PROVIDERS` and `sre_agent.cluster_context.SUPPORTED_LLM_PROVIDERS` restrict model operations to `anthropic` and `gemini`. `validate_startup_config` verifies credentials at CLI/container boot before database migrations or web server startup.
- **Single Canonical Runner:** All production callers (`sre_agent/job_worker.py`, `mission_control.py`) invoke the LangGraph incident pipeline strictly through `sre_agent.incident_runner.run_incident_investigation`. Historical `sre_agent/agent_runtime_tasks.py` is quarantined as a forwarding shim.
- **Durable Job Worker Pipeline:** Incidents and investigations run as PostgreSQL lease-backed durable jobs with heartbeat renewals, bounded retry attempts, cancellation, and dead-letter queueing (`sre_agent/job_worker.py`).
- **Unified ORM & Migration Linearity:** All models inherit from `backend.models.Base`. Audit storage uses `AgentAuditLog` (R06 schema with composite timestamp indexes, superseding P07). Alembic maintains a strict single-head chain terminating at `2253eabf13e3` (`add_cluster_heartbeat_truth`). Obsolete revisions `a9b0c1d2e3f4`, `e6f7a8b9c0d1`, and `d5e6f7a8b9c0` must not be restored.
- **Distributed Live Events:** `sre_agent.live_events` multiplexes incident lifecycle notifications across API replicas via Redis pub/sub with an in-memory fallback.
- **Evidence-Based Severity & Fail-Closed Logic:** `sre_agent.severity_engine` derives incident severity solely from measured evidence links (`EvidenceLink`). Missing telemetry escalates to `UNKNOWN` or higher severity; it never fabricates calm values.
- **Mutation Gateway & Safety:** Cluster writes pass `sre_agent.mutation_gateway` with namespace constraints, tenant isolation, idempotency locks, approval interrupts, and audit logs.
- **Verified Learning:** `sre_agent.act_phase` mandates verified objective resolution before skills can be promoted to `skill_store`. Uncalibrated confidence fails closed to requiring human approval.
- **Release Evaluation Contract:** `benchmarks/release_gate.py` gates prompt, model, and tool changes against content-addressed statistical and adversarial evidence bundles.
- **Module Reachability Governance:** `scripts/check_module_reachability.py` ensures no unmanaged top-level modules exist in `sre_agent/`. Scaffolding modules (`agent_audit`, `models`, `actor_runtime`, `code_sandbox`, `terminal_agent`, `toolsets`) are tracked in `EXPERIMENTAL`.

## Completed or verified work
- Fully merged all 41 backlog work packages and PR #34 to `master`.
- Restored namespace enforcement for MCP reads and query selectors, including blocking namespace enumeration.
- Repaired scoped audit persistence, per-cluster LLM credential validation, canonical durable runner arguments, recurring job-lease renewal, and durable mission-control follow-ups.
- Restricted the investigation worker to investigation jobs, added release-evaluation and Terraform to the aggregate CI gate, and isolated generated runbooks in ACT integration tests.

## Active problem
Resolved locally. `benchmarks/release/candidate/bundle.json` was regenerated (`change_class: "tool"`, `candidate.source_digest` recomputed via `release_gate.py digest`) to match the protected `sre_agent/mcp_tool_wrapper.py` change on `codex/post-integration-regression-fixes`. `release_gate.py impact` against `master..HEAD` now reports `PROMOTE`. PR #42 (`codex/post-integration-operational-fixes`, non-protected fixes) is open, `MERGEABLE`/`CLEAN`, all CI checks green, not yet merged. This branch is not yet pushed or opened as a PR.

## Relevant files
- `sre_agent/provider_config.py` & `sre_agent/constants.py` (Supported LLM providers)
- `sre_agent/incident_runner.py` (Canonical entrypoint)
- `sre_agent/job_worker.py` (Durable job runner)
- `sre_agent/namespace_scope.py` & `sre_agent/mcp_tool_wrapper.py` (Read isolation and scoped audit)
- `sre_agent/api/v1/mission_control.py` (Durable follow-up queueing)
- `sre_agent/severity_engine.py` & `sre_agent/act_phase.py` (Severity & ACT execution)
- `backend/models.py` & `backend/alembic/versions/` (Database schemas & migrations)
- `benchmarks/release_gate.py` (Release evaluation contracts)
- `scripts/check_python_quality.sh` & `scripts/check_module_reachability.py` (CI validation)

## Verification commands and latest results
- `uv run pytest -q` -> 673 passed in 3.92s; tracked runbook artifacts remained unchanged.
- `bash scripts/check_python_quality.sh` -> Ruff critical, Mypy, and compileall pass.
- `uv run python scripts/check_module_reachability.py` -> 67 reachable, 6 experimental.
- `bash scripts/check_no_static_secrets.sh` -> Secret scan passed.
- `bash scripts/check_eval_smoke.sh` -> 33 passed.
- `bash scripts/check_helm_production.sh` & `check_terraform.sh` -> Manifest & Terraform checks pass.
- Release matrix -> PASS; candidate bundle -> PROMOTE in isolation; `release_gate.py impact` against `master..HEAD` -> PROMOTE.
- `uv run pytest -q` (full suite, re-verified after the bundle fix) -> 674 passed in 4.05s.

## Known blockers or risks
- None outstanding for this fix. Merging PR #42 and pushing/opening a PR for `codex/post-integration-regression-fixes` are pending user confirmation (shared-state actions), not technical blockers.

## Next bounded task
- Merge PR #42, then push `codex/post-integration-regression-fixes` (already rebased on the post-merge `master` since it already contains PR #42's commit) and open its PR; CI's "Release evaluation contract" job should reproduce the local `PROMOTE` result.
