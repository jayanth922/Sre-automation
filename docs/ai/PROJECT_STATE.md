# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10, the reconciled migration chain, and R02 durable jobs are merged into
`master`. PR #18 is the next serialized operational change and contains R06
durable audit behavior on the current integration base.

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point.
- Authenticated resources are organization-scoped; live writes pass the
  mutation gateway with fresh policy, tenant, lock, confidence, idempotency,
  approval, and audit checks.
- Run provenance, recovery, grading, statistics, calibration, adversarial
  safety, trace accounting, release evaluation, and learning fail closed when
  objective evidence is missing.
- Successful learning requires externally verified recovery.
- Investigation work is persisted as tenant-scoped jobs with idempotency,
  lease ownership, heartbeats, bounded retries, cancellation, and dead-letter
  state.
- Agent flight-recorder writes use canonical `AgentAuditLog`; write failures
  degrade the job instead of disappearing.
- Alembic has one linear head; operational branches must not restore obsolete
  revision files after reconciliation.

## Completed or verified work
- R06 owns `agent_audit_logs` and supersedes P07.
- The migration chain merged through PR #40 is:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- R02 merged through PR #13.
- R06 adds scoped audit context, durable tool audit records, visible write
  failures, retention/export helpers, and mission-control querying.
- R06's obsolete `e6f7a8b9c0d1` migration is excluded; the reconciled
  `d3ac85ffcc7d` migration remains the schema owner.
- The R06/runtime conflict preserves both degraded-audit reporting and
  model-accounting/trace-completeness result fields.

## Active problem
PR #18 is mergeable and has no unresolved review threads. Backend, frontend,
manifests, and image checks pass, but the release-evaluation contract blocks
because R06 changes protected tool paths without candidate release evidence.
R07 operational reconciliation must follow R06.

## Relevant files
- `backend/models.py`
- `backend/alembic/versions/d3ac85ffcc7d_add_agent_audit_logs.py`
- `sre_agent/agent_audit.py`, `sre_agent/audit_context.py`
- `sre_agent/agent_nodes.py`, `sre_agent/agent_runtime.py`
- `sre_agent/api/v1/mission_control.py`, `sre_agent/mcp_tool_wrapper.py`
- `tests/test_agent_audit.py`

## Verification commands and latest results
- `.venv/bin/python -m alembic heads`: one head, `2253eabf13e3`.
- Audit, model-accounting, and trace-evidence focused suites: 21 passed with 13
  existing warnings.
- Ruff on the new R06 audit modules and focused test: passed.
- PR #18 at `f017a1d`: four product checks passed; release evaluation blocked
  with `protected prompt/model/tool change lacks release evidence`.

## Known blockers or risks
- No live database upgrade/downgrade or retention purge has been exercised.
- Audit failure signaling depends on process-local context propagation around
  each investigation.
- The release gate requires at least 20 paired baseline/candidate trials plus
  linked adversarial and root-trace artifacts from a live benchmark system.
  The repository's frozen safe fixture is explicitly not production evidence,
  and no local Sentinel benchmark API was available at `localhost:8080`.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
Generate a real R06 candidate evidence bundle from a live baseline/candidate
benchmark environment, or explicitly defer the protected tool instrumentation.
Then rerun PR #18 and proceed to R07 while dropping `d5e6f7a8b9c0`.
