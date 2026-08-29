# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Runtime PRs open: R02 #13, R03 #14, R08 #15, R04 #16. R07 truthful heartbeats
is implemented on `codex/r07-truthful-heartbeats`.

## Current architecture and invariants
- Cluster connectivity status is derived only from observed heartbeats
  (Alertmanager webhook, edge runtime pulse). Reconcile never fabricates
  freshness. Statuss: online / degraded / stale / offline / maintenance.
- Health API exposes source, reason, and age.

## Completed or verified work
- R07: heartbeat evaluator, migration for source/reason, replace synthetic
  online loop, alertmanager + edge recording, dashboard health fields, tests.

## Active problem
A01–A10 and earlier runtime PRs still await merge.

## Relevant files
- `sre_agent/cluster_heartbeat.py`, `backend/crud.py`,
  `sre_agent/agent_runtime.py`, `tests/test_cluster_heartbeat.py`

## Verification commands and latest results
- `pytest tests/test_cluster_heartbeat.py`: 5 passed

## Known blockers or risks
- Alertmanager-only tenants go stale without webhooks; edge tenants need
  `CLUSTER_TOKEN` runtime for edge pulses.

## Next bounded task
Open R07 PR, then R06 durable audit model or Redis-backed shared admission.
