# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
All 41 work packages from the Sentinel build backlog are fully integrated, merged into `master`, and passing all 17 CI quality gate jobs.

### Integrated Backlog Tracks (41 Work Packages)
- **Foundation (T01–T10):** Multi-agent LangGraph core, MCP adapters, state store, observability, and evaluation baseline.
- **Evaluation & Guardrails (A01–A10):** Run provenance (A01), recovery grading (A02), MTTR/diagnostics statistics (A03–A04), task-specific confidence calibration (A05), prompt-injection & adversarial safety (A06–A07), trace accounting (A08), release evaluation gates (A09), and verified-only learning (A10).
- **Robustness & Operations (R01–R11):** Mutation gateway & locks (R01), durable job leases (R02), namespace isolation (R03), per-cluster model routing (R04), canonical graph runner (R05), canonical audit log storage & retention (R06), truthful cluster heartbeat (R07), fail-closed admission concurrency (R08), distributed live event bus (R09), evidence-based severity engine (R10), and external incident/PR loops (R11).
- **Production Platform (P01–P11):** Provider config defaults (P01), typed settings (P02), websocket routing (P03), production Helm chart (P04), Terraform Helm module (P05), generic platform overlays (P06), ORM model consolidation (P07), CI quality gates (P08), integration test layers (P09), dead module reachability enforcement (P10), and truthful documentation & benchmark fixtures (P11).

## Current architecture and invariants
- **Single Canonical Runner:** All production callers (`sre_agent/job_worker.py`, `mission_control.py`) invoke the LangGraph incident pipeline strictly through `sre_agent.incident_runner.run_incident_investigation`. Historical `sre_agent/agent_runtime_tasks.py` is quarantined as a forwarding shim.
- **Durable Job Worker Pipeline:** Incidents and investigations run as PostgreSQL lease-backed durable jobs with heartbeat renewals, bounded retry attempts, cancellation, and dead-letter queueing (`sre_agent/job_worker.py`).
- **Unified ORM & Migration Linearity:** All models inherit from `backend.models.Base`. Audit storage uses `AgentAuditLog` (R06 schema with composite timestamp indexes, superseding P07). Alembic maintains a strict single-head chain terminating at `2253eabf13e3` (`add_cluster_heartbeat_truth`). Obsolete revisions `a9b0c1d2e3f4`, `e6f7a8b9c0d1`, and `d5e6f7a8b9c0` must not be restored.
- **Distributed Live Events:** `sre_agent.live_events` multiplexes incident lifecycle notifications across API replicas via Redis pub/sub with an in-memory fallback.
- **Evidence-Based Severity & Fail-Closed Logic:** `sre_agent.severity_engine` derives incident severity solely from measured evidence links (`EvidenceLink`). Missing telemetry escalates to `UNKNOWN` or higher severity; it never fabricates calm values.
- **Mutation Gateway & Safety:** Cluster writes pass `sre_agent.mutation_gateway` with namespace constraints, tenant isolation, idempotency locks, approval interrupts, and audit logs.
- **Verified Learning:** `sre_agent.act_phase` mandates verified objective resolution before skills can be promoted to `skill_store`. Uncalibrated confidence fails closed to requiring human approval.
- **Module Reachability Governance:** `scripts/check_module_reachability.py` ensures no unmanaged top-level modules exist in `sre_agent/`. Scaffolding modules (`agent_audit`, `models`, `actor_runtime`, `code_sandbox`, `terminal_agent`, `toolsets`) are tracked in `EXPERIMENTAL`.

## Completed or verified work
- Fully merged all 41 work packages from the build backlog to `master`.
- Resolved post-merge semantic conflicts on `master`:
  - Removed duplicate P07 migration and model; preserved canonical R06 schema and Alembic head.
  - Quarantined `agent_runtime_tasks.py` and routed `job_worker.py` through the canonical runner facade.
  - Added `agent_audit` and `models` to the reachability `EXPERIMENTAL` set.
  - Aligned unit and integration test telemetry with R10 evidence structures and A10 confidence calibration fixtures.
  - Fixed duplicate imports in `mission_control.py` and satisfied Mypy strict return typing in `execution_context.py`.
- Verified green CI across all 17 GitHub Actions jobs (Run #33288436827).

## Active problem
None. All PRs merged, all tests passing, and all CI gates green on `master`.

## Relevant files
- `sre_agent/incident_runner.py` (Canonical entrypoint)
- `sre_agent/job_worker.py` (Durable job runner)
- `sre_agent/severity_engine.py` & `sre_agent/act_phase.py` (Severity & ACT execution)
- `backend/models.py` & `backend/alembic/versions/` (Database schemas & migrations)
- `scripts/check_python_quality.sh` & `scripts/check_module_reachability.py` (CI validation)

## Verification commands and latest results
- `uv run pytest -q` -> 661 passed in 4.26s.
- `bash scripts/check_python_quality.sh` -> Ruff critical, Mypy, and compileall pass.
- `uv run python scripts/check_module_reachability.py` -> 67 reachable, 6 experimental.
- `bash scripts/check_no_static_secrets.sh` -> Secret scan passed.
- `bash scripts/check_eval_smoke.sh` -> Eval smoke passed.
- GitHub Actions CI -> All 17 jobs passed.

## Known blockers or risks
- Future contributions must comply with CI requirements: >=20% test coverage, strict Ruff critical rules, Mypy on curated modules, single Alembic head, and module reachability tracking.

## Next bounded task
- Ready for new feature development or operational tasks.
