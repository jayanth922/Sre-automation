# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Runtime PRs through R09 open. R10 evidence-based severity is on
`codex/r10-evidence-severity`.

## Current architecture and invariants
- Severity uses measured features only (error rate, burn, breadth, saturation,
  confidence). Missing telemetry → `Severity.UNKNOWN` / approval required.
- Alert `critical` labels never invent error/burn values; agent_result key counts
  are not blast radius.
- Every feature can carry an `EvidenceLink` (field/source/value/observed_at).

## Completed or verified work
- R10: typed extractor, UNKNOWN escalation, policy/mutation UNKNOWN handling,
  act-report evidence fields, tests.

## Active problem
Earlier runtime/A-stack PRs still unmerged. R11 external loops remain open.

## Relevant files
- `sre_agent/severity_engine.py`, `sre_agent/act_phase.py`,
  `sre_agent/policy_gate.py`, `tests/test_severity_engine.py`

## Verification commands and latest results
- `pytest tests/test_severity_engine.py tests/test_act_phase.py`: passed

## Known blockers or risks
None for R10 unit scope; live PromQL extraction richness can grow later.

## Next bounded task
Open R10 PR; then R11 external incident/remediation loop closure.
