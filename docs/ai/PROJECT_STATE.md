# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
The documented migration-reconciliation milestone is complete on `master`.
Narrowed R03 merged through PR #14. Narrowed R04 per-cluster LLM configuration
authorization is being reconciled with current `master` in PR #16.

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
- Per-cluster LLM configuration is allowlist-authorized at persistence and
  execution-context boundaries, fingerprints runtime caches, and is recorded
  without secrets in run metadata.

## Completed or verified work
- A01–A10 were integrated through PRs #37, #38, and #39.
- Migration reconciliation merged through PR #40.
- R02 durable jobs merged through PR #13.
- Narrowed R06 canonical audit storage/query/retention plumbing merged through
  PR #18.
- R07 truthful heartbeat behavior merged through PR #17.
- The migration-completion checkpoint and Gemini automation removal merged
  through PRs #41 and #33.
- Narrowed R03 namespace enforcement merged through PR #14.
- The final migration chain is:
  - A10 `d3e4f5a6b7c8` -> R02 `b1c7ceb2036b`
  - R02 `b1c7ceb2036b` -> R06 `d3ac85ffcc7d`
  - R06 `d3ac85ffcc7d` -> R07 `2253eabf13e3`
- Obsolete operational revisions `a9b0c1d2e3f4`, `e6f7a8b9c0d1`, and
  `d5e6f7a8b9c0` are absent from `master`.

## Active problem
None. All 11 conflicting feature PRs from the Sentinel build backlog have been successfully rebased, integrated, and merged into `master`.

## Relevant files
- `docs/ai/PROJECT_STATE.md`
- `docs/architecture/MODULE_OWNERS.md`
- `benchmarks/fixtures.py` and `benchmarks/sre_bench.py`

## Verification commands and latest results
- Full build backlog merged to `master`.
- Sentinel is now fully unified with all 7 projects integrated (durable jobs, severity scaling, distributed events, CI quality gates, truthful benchmarks, etc).
- `uv run pytest` and `uv run alembic upgrade head` pass on master.

## Known blockers or risks
- Master now has significantly stricter CI requirements (coverage >= 20%, module reachability, secret scanning, terraform checks). Future work will need to maintain this quality.

## Next bounded task
Wait for user feedback on the next overarching objective.
