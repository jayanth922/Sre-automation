# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10 and the reconciled migration chain are merged into `master`. PR #13
contains the R02 durable-job implementation, targets `master`, and is being
refreshed onto the final integration base before merge.

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point.
- Authenticated resources are organization-scoped; live writes pass the
  mutation gateway with fresh policy, tenant, lock, confidence, idempotency,
  approval, and audit checks.
- Run provenance, recovery, grading, statistics, calibration, adversarial
  safety, trace accounting, release evaluation, and learning fail closed when
  objective evidence is missing.
- Successful learning requires externally verified recovery.
- Alembic has one linear head; operational branches must not restore their old
  revision files after reconciliation.
- Investigation work is persisted as tenant-scoped jobs with idempotency,
  lease ownership, heartbeats, bounded retries, cancellation, and dead-letter
  state rather than process-local background dispatch.

## Completed or verified work
- R06 owns `agent_audit_logs` and supersedes P07.
- The migration chain merged through PR #40 is:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- R02 adds the durable job model, claim/heartbeat/fail/complete adapters,
  tenant-fair worker loop, alert/manual/self-defense enqueue paths, and cancel
  endpoint.
- R02 retains A10's run-manifest response and manifest comparison endpoints
  while adding durable-job response fields.
- R02's obsolete `a9b0c1d2e3f4` migration is excluded; the reconciled
  `b1c7ceb2036b` migration remains the schema owner.

## Active problem
PR #13 has been retargeted to `master`. Its branch must include current
`master`, pass fresh product CI, and be confirmed conflict-free before the user
merges it. R06 and R07 still require serial operational reconciliation.

## Relevant files
- `sre_agent/durable_jobs.py`, `sre_agent/job_store.py`,
  `sre_agent/job_worker.py`
- `sre_agent/agent_runtime.py`, `sre_agent/api/v1/alerts.py`,
  `sre_agent/api/v1/incidents.py`, `sre_agent/api/v1/jobs.py`
- `backend/models.py`, `backend/schemas.py`
- `backend/alembic/versions/b1c7ceb2036b_add_durable_job_leases.py`
- `tests/test_durable_jobs.py`

## Verification commands and latest results
- Migration reconciliation: one Alembic head; compile, Ruff, Black, and
  whitespace checks passed.
- `uv run --frozen --extra dev pytest -q`: 529 passed, 13 existing warnings.
- Durable-job plus run-manifest focused suites: 11 passed.
- R02's four new Python files pass Ruff and Black; backend and agent modules
  byte-compile successfully.
- Product CI was green against the reconciliation base; fresh `master`-based
  CI is required after the ancestry refresh.

## Known blockers or risks
- No live Postgres migration, `FOR UPDATE SKIP LOCKED` contention, API-process
  kill/reclaim, or multi-replica restart has been exercised.
- The in-process worker is replica-safe only to the extent guaranteed by the
  database lease implementation.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
Push the R02 ancestry refresh, verify fresh product CI and review state, then
have the user merge PR #13. Next reconcile R06 operational code while dropping
`e6f7a8b9c0d1`.
