# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10, the reconciled migration chain, R02 durable jobs, and R06 canonical
audit storage are merged into `master`. PR #17 is the next serialized
operational change and contains R07 truthful cluster heartbeat behavior on the
current integration base.

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point.
- Authenticated resources are organization-scoped; live writes pass the
  mutation gateway with fresh policy, tenant, lock, confidence, idempotency,
  approval, and audit checks.
- Run provenance, recovery, grading, statistics, calibration, adversarial
  safety, trace accounting, release evaluation, and learning fail closed when
  objective evidence is missing.
- Successful learning requires externally verified recovery.
- Investigation work uses tenant-scoped durable jobs with leases, bounded
  retries, cancellation, and dead-letter state.
- Agent flight-recorder storage uses canonical `AgentAuditLog`.
- Cluster connectivity is derived only from observed heartbeats; reconciliation
  must never manufacture freshness.
- Alembic has one linear head; operational branches must not restore obsolete
  revision files after reconciliation.

## Completed or verified work
- R06 owns `agent_audit_logs` and supersedes P07.
- The migration chain merged through PR #40 is:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- R02 merged through PR #13; narrowed R06 merged through PR #18.
- R07 derives online, degraded, stale, offline, and maintenance status from
  heartbeat age and exposes source, reason, age, and last-observed time.
- Alertmanager webhooks and edge runtime pulses record observed heartbeats; the
  reconciliation loop only ages recorded observations.
- R07's obsolete `d5e6f7a8b9c0` migration is excluded; reconciled
  `2253eabf13e3` remains the schema owner.

## Active problem
PR #17 is the next merge candidate. Its serialized scope is locally verified;
fresh product CI, mergeability, and review state must be confirmed before the
user merges it.

## Relevant files
- `sre_agent/cluster_heartbeat.py`
- `sre_agent/agent_runtime.py`
- `sre_agent/api/v1/alerts.py`, `sre_agent/api/v1/clusters.py`,
  `sre_agent/api/v1/services.py`
- `backend/crud.py`, `backend/models.py`, `backend/schemas.py`
- `backend/alembic/versions/2253eabf13e3_add_cluster_heartbeat_truth.py`
- `dashboard/components/dashboard/AgentStatus.tsx`, `dashboard/lib/console.ts`
- `tests/test_cluster_heartbeat.py`

## Verification commands and latest results
- `.venv/bin/python -m alembic heads`: one head, `2253eabf13e3`.
- R07 heartbeat plus adjacent R02/R06 focused suites: 16 passed.
- Ruff on the R07 heartbeat module and focused test: passed.
- Local release impact: `NOT_REQUIRED`.

## Known blockers or risks
- Alertmanager-only tenants become stale without webhooks; edge tenants need a
  configured `CLUSTER_TOKEN` for runtime pulses.
- No live clock-aging, webhook outage, or multi-replica heartbeat reconciliation
  test has been exercised.
- Protected R06 tool instrumentation remains deferred pending real release
  evidence.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
Have the user merge PR #17 after fresh checks pass. Then continue with the next
independently rooted operational PR.
