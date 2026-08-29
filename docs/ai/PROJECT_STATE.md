# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01 is merged and A02–A10 remain stacked. PR #35 provides the migration-only
integration base after A10. R02's operational durable-job implementation is now
stacked on that reconciliation base without its obsolete migration.

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
  revision files after rebasing.
- Investigation work is persisted as tenant-scoped jobs with idempotency,
  lease ownership, heartbeats, bounded retries, cancellation, and dead-letter
  state rather than process-local background dispatch.

## Completed or verified work
- PR #35 starts directly from `codex/a10-verified-learning` and serializes:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- R06 owns `agent_audit_logs` and supersedes P07.
- R02 adds the durable job model, claim/heartbeat/fail/complete adapters,
  tenant-fair worker loop, alert/manual/self-defense enqueue paths, and cancel
  endpoint.
- R02 retains A10's run-manifest response and manifest comparison endpoints
  while adding durable-job response fields.
- R02's obsolete `a9b0c1d2e3f4` migration is excluded; the reconciled
  `b1c7ceb2036b` migration remains the schema owner.

## Active problem
PR #13 is stacked on PR #35 and must not be retargeted to `master` until the
reconciliation PR merges. R06 and R07 still require the same serial operational
reconciliation. Other R/P branches remain independently rooted and require
later bounded integration.

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
- Current-base release impact reports `NOT_REQUIRED` with no protected changes.

## Known blockers or risks
- No live Postgres migration, `FOR UPDATE SKIP LOCKED` contention, API-process
  kill/reclaim, or multi-replica restart has been exercised.
- The in-process worker is replica-safe only to the extent guaranteed by the
  database lease implementation.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
After the A-stack and PR #35 merge, retarget PR #13 to `master` and merge it if
checks remain green. Then reconcile R06 operational code while dropping
`e6f7a8b9c0d1`.
