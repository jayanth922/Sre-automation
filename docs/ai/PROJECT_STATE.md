# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10 are the integration base. Branch `codex/migration-reconciliation` now
serializes the Alembic changes needed by R02, R06, and R07 before their
operational implementations are rebased.

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

## Completed or verified work
- Migration reconciliation starts directly from `codex/a10-verified-learning`
  at `c3f314e`.
- R06 owns `agent_audit_logs` and supersedes P07. The retained R06 schema adds
  organization, cluster, incident, and run provenance; P07's narrower duplicate
  migration is excluded.
- Old operational revisions are replaced by this linear chain:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- Alembic resolves one head, and all three migration files pass Python compile,
  Ruff, Black, and whitespace checks.

## Active problem
R02, R06, and R07 still independently target the old `master` and still carry
their obsolete revision files. Their operational commits must be rebased
serially onto the reconciliation base while dropping those old migrations.
Other R/P branches also remain independently rooted and require later bounded
integration.

## Relevant files
- `backend/alembic/versions/b1c7ceb2036b_add_durable_job_leases.py`
- `backend/alembic/versions/d3ac85ffcc7d_add_agent_audit_logs.py`
- `backend/alembic/versions/2253eabf13e3_add_cluster_heartbeat_truth.py`
- Operational branches: `codex/r02-durable-jobs`,
  `codex/r06-durable-audit`, and `codex/r07-truthful-heartbeats`

## Verification commands and latest results
- `.venv/bin/python -m alembic heads`: one head, `2253eabf13e3`.
- `.venv/bin/python -m alembic history -r d3e4f5a6b7c8:heads`: serialized A10,
  R02, R06, R07 ancestry in that order.
- `.venv/bin/ruff check <three migration files>`: passed.
- `.venv/bin/black --check <three migration files>`: passed.
- `python3 -m py_compile <three migration files>`: passed.

## Known blockers or risks
- No live database upgrade or downgrade has been exercised.
- R02/R06/R07 model and runtime changes are intentionally absent from the
  migration-only reconciliation branch.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
Rebase the R02 operational implementation onto `codex/migration-reconciliation`,
drop its obsolete `a9b0c1d2e3f4` migration, and run focused durable-job tests.
Then integrate R06 and R07 serially, dropping `e6f7a8b9c0d1` and
`d5e6f7a8b9c0` respectively.
