# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
All 41 work packages from the Sentinel Claude build backlog (T01–T10, A01–A10, R01–R11, P01–P11) are fully integrated, merged, and passing all 17 CI checks on `master`.

## Current architecture and invariants
- **Single Canonical Runner:** All production callers (durable `job_worker`, `mission_control`) execute incident graphs via `sre_agent.incident_runner.run_incident_investigation`. Historical `agent_runtime_tasks.py` is a quarantined forwarder.
- **Durable Job Leases:** Incidents and investigations run as tenant-scoped durable jobs in PostgreSQL with heartbeat leases, bounded retries, and dead-letter handling (`sre_agent/job_worker.py`).
- **Unified ORM & Migrations:** Canonical models live exclusively in `backend.models.Base`. Flight-recorder audit storage uses `AgentAuditLog` (R06 schema with multi-column indexes). Alembic has a single linear chain ending at head `2253eabf13e3`.
- **Distributed Live Events:** `sre_agent.live_events` provides pub/sub across API replicas with Redis backend and in-memory fallback.
- **Fail-Closed Evidence Severity:** `sre_agent.severity_engine` evaluates impact and urgency from measured evidence links (`EvidenceLink`). Missing telemetry escalates to `UNKNOWN` or higher severity rather than fabricating calm metrics.
- **Mutation & Safety Gate:** Live cluster writes pass `sre_agent.mutation_gateway` with namespace constraints, tenant isolation, idempotency keys, approval interrupts, and audit logging.
- **Verified Learning & Confidence Calibration:** `sre_agent.act_phase` requires verified objective resolution before promoting skills to `skill_store`. Uncalibrated confidence fails closed to requiring human approval.
- **External Loops:** `sre_agent.alert_resolution` and `edge_mcp_servers/mcp_servers/github_exec` manage Alertmanager lifecycle reconciliation and guardrailed GitHub PR rollback workflows.
- **Quality Gates:** CI enforces Ruff critical checks (E9/F63/F7/F82/F821/F823/F811), Mypy typing on curated core, module reachability (`scripts/check_module_reachability.py`), >=20% test coverage, and static secret scanning.

## Completed or verified work
- Merged and unified all 41 work packages across Foundation (T01–T10), Evaluation & Guardrails (A01–A10), Robustness & Operations (R01–R11), and Production Platform (P01–P11).
- Resolved semantic merge conflicts on `master`: eliminated duplicate P07 migration and ORM models, reconciled R05 canonical runner with R02 durable jobs, aligned module reachability allowlists, and modernized test suites for R10 evidence and A10 confidence calibration.
- Cleaned up python typing and import lint rules.
- Verified 100% green CI across all 17 jobs in GitHub Actions (Run #33288436827).

## Active problem
None. The repository is in a healthy, fully green state on `master`.

## Relevant files
- `sre_agent/incident_runner.py` (Canonical entry point)
- `sre_agent/job_worker.py` (Durable background worker)
- `sre_agent/severity_engine.py` & `sre_agent/act_phase.py` (Severity & ACT pipeline)
- `backend/models.py` & `backend/alembic/versions/` (Database schema & migrations)
- `scripts/check_python_quality.sh` & `scripts/check_module_reachability.py` (CI gates)

## Verification commands and latest results
- `uv run pytest -q` -> 661 passed in 4.26s.
- `bash scripts/check_python_quality.sh` -> Ruff, Mypy, and compileall pass.
- `uv run python scripts/check_module_reachability.py` -> 67 reachable, 6 experimental.
- `bash scripts/check_no_static_secrets.sh` -> Secret scan passed.
- GitHub Actions CI -> All 17 jobs passed.

## Known blockers or risks
- Master has strict quality gates (coverage >= 20%, reachability checks, Mypy on core models). Future PRs must follow these standards.

## Next bounded task
- Ready for new user feature requests or operational tasks.
