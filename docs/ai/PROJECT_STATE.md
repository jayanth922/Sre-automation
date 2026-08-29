# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
The documented migration-reconciliation milestone is complete on `master`.
Narrowed R03 execution-context and mutation namespace enforcement is being
reconciled with current `master` on `codex/r03-namespace-enforcement` for PR #14.

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point.
- Authenticated resources are organization-scoped; live writes pass the
  mutation gateway with fresh policy, tenant, lock, confidence, idempotency,
  approval, and audit checks.
- Run provenance, recovery, grading, statistics, calibration, adversarial
  safety, trace accounting, release evaluation, and learning fail closed when
  objective evidence is missing.
- Investigation work uses tenant-scoped durable jobs with leases, bounded
  retries, cancellation, and dead-letter state.
- Agent flight-recorder storage uses canonical `AgentAuditLog`; R06 owns its
  schema and supersedes P07.
- Cluster connectivity is derived only from observed heartbeats; reconciliation
  never manufactures freshness.
- Alembic has one linear head; operational branches must not restore obsolete
  revision files.
- Configured cluster namespace is required in API/production runtime; the
  mutation gateway injects the primary namespace when absent and rejects
  cross-namespace targets.

## Completed or verified work
- A01–A10 were integrated through PRs #37, #38, and #39.
- Migration reconciliation merged through PR #40.
- R02 durable jobs merged through PR #13.
- Narrowed R06 canonical audit storage/query/retention plumbing merged through
  PR #18.
- R07 truthful heartbeat behavior merged through PR #17.
- The migration-completion checkpoint and Gemini automation removal merged
  through PRs #41 and #33.
- The final migration chain is:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- Obsolete operational revisions `a9b0c1d2e3f4`, `e6f7a8b9c0d1`, and
  `d5e6f7a8b9c0` are absent from `master`.

## Active problem
PR #14 has been reconciled locally with current `master`; it needs the merge
commit pushed and fresh GitHub CI before it can be merged.

## Relevant files
- `backend/alembic/versions/b1c7ceb2036b_add_durable_job_leases.py`
- `backend/alembic/versions/d3ac85ffcc7d_add_agent_audit_logs.py`
- `backend/alembic/versions/2253eabf13e3_add_cluster_heartbeat_truth.py`
- `sre_agent/durable_jobs.py`, `sre_agent/job_store.py`,
  `sre_agent/job_worker.py`
- `sre_agent/agent_audit.py`, `sre_agent/audit_context.py`
- `sre_agent/cluster_heartbeat.py`
- `sre_agent/namespace_scope.py`, `sre_agent/execution_context.py`,
  `sre_agent/mutation_gateway.py`, `tests/test_namespace_scope.py`

## Verification commands and latest results
- `.venv/bin/python -m alembic heads`: one head, `2253eabf13e3`.
- `.venv/bin/python -m alembic history -r d3e4f5a6b7c8:heads`: serialized
  R02 → R06 → R07 ancestry.
- R07 plus adjacent R02/R06 focused suites: 16 passed.
- Product CI passed for PRs #13, #18, and #17 before merge.
- Narrowed R03 focused suite after reconciliation: 21 passed.
- Full local test suite after reconciliation: 544 passed; byte-compilation
  succeeded.
- Frozen release-evaluation matrix: `PASS`.
- R03 release-impact check: `NOT_REQUIRED`; no protected paths remain in the
  final PR diff.

## Known blockers or risks
- No live database upgrade/downgrade, multi-replica job contention, audit
  retention purge, or heartbeat outage/aging test has been exercised.
- Protected R06 tool-execution instrumentation remains deferred; it requires a
  separate candidate-evidence bundle with paired, adversarial, and root-trace
  artifacts.
- Protected MCP tool-call namespace enforcement is deferred; it requires a
  separate candidate-evidence bundle. RBAC at the edge server remains a
  separate enforcement layer.
- Preserve the three pre-existing untracked artifacts: Terraform's lockfile and
  two generated runbooks.

## Next bounded task
Push PR #14, verify fresh GitHub mergeability and CI, then hand it to the user
to merge before starting the next independently rooted operational PR.
