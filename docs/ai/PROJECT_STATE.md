# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Runtime PRs: R02 #13, R03 #14, R08 #15, R04 #16, R07 #17. R06 durable audit is
implemented on `codex/r06-durable-audit`.

## Current architecture and invariants
- Agent flight-recorder (`agent_audit_logs`) lives on the canonical Alembic Base
  with org/cluster/incident/run indexes. Write failures mark the job `degraded`
  instead of disappearing. Retention via `AGENT_AUDIT_RETENTION_DAYS` (default 90).

## Completed or verified work
- R06: model + migration, scoped audit context, visible write failures, export/
  purge helpers, mission-control query without silent empty fallback.

## Active problem
A01–A10 and earlier runtime PRs still await merge. R06 and R07 both revise the
same Alembic parent and will need a merge revision when both land.

## Relevant files
- `backend/models.py` (`AgentAuditLog`), `backend/alembic/versions/e6f7a8b9c0d1_*`,
  `sre_agent/audit_context.py`, `sre_agent/agent_audit.py`,
  `tests/test_agent_audit.py`

## Verification commands and latest results
- `pytest tests/test_agent_audit.py`: 6 passed

## Known blockers or risks
- Alembic head conflict with R07 (`d5e6f7a8b9c0`) until a merge revision exists.

## Next bounded task
Open R06 PR; then R05 runner consolidation or Redis-backed shared admission.
