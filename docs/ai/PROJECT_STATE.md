# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. T01–T10, R01, security fixes, and P03 routing are merged to `master`
through PRs #1 and #2. A01 immutable run provenance is open in PR #3. A02–A09
are stacked local commits on A01. A10 verified-only learning is in progress on
`codex/a10-verified-learning`.

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point; graph
  completion or a self-authored summary is not recovery evidence.
- Authenticated v1 resources are organization-scoped. Legacy global runtime
  routes require `INTERNAL_API_TOKEN`; WebSockets require short-lived scoped
  tickets and reject cross-organization incidents/events.
- Graphs, tools, and MCP clients use immutable tenant `ExecutionContext`.
- Credentials use versioned AES-GCM storage and hashed lookup. MCP transport is
  authenticated and local edge ports bind to loopback.
- Non-autonomous actions use durable, exact-action approvals. Every live write
  passes the mutation gateway's fresh lock, policy, tenant, confidence,
  idempotency, and audit checks.
- Every incident job persists an immutable run manifest before graph execution.
  Missing provenance makes comparisons invalid.
- Benchmark recovery comes from scenario-owned Prometheus probes. Structured
  grades, statistical gates, confidence calibration, adversarial gates, and
  release gates fail closed when evidence is absent, incomparable, or
  uncalibrated.
- Alerts, logs, tool results, runbooks, repository text, retrieved memory, and
  prior model output are untrusted evidence, never approval or authority.
- Prompt, model, and tool changes that match protected path rules require a
  pinned release evidence bundle whose source digest matches the repository.
- Memory, skills, and successful runbooks promote only after live execution plus
  objective RESOLVED verification. Dry-run, blocked, failed, and unknown
  outcomes become negative exemplars and never successful ones; invalidated
  skills are excluded from proposals.

## Completed or verified work
- T01–T10/R01 through A09 as previously checkpointed, including the A09 release
  evaluation gate (`4319f8f`).
- A10 (in progress): `verified_learning` eligibility/provenance APIs; skill
  store provenance, negatives, and invalidation; ACT/graph/runtime/supervisor
  gates so dry-run and unverified outcomes cannot become successful memory,
  skills, or runbooks.

## Active problem
Live Meridian, blinded labels, paired candidate trials, real calibration,
adversarial-model observations, live traces, and production release bundles are
still required. A10 still needs docs/canvas checkpointing and a commit after
focused verification.

## Relevant files
- A09: `benchmarks/release_gate.py`, `benchmarks/release/v1/`, CI job.
- A10: `sre_agent/verified_learning.py`, `sre_agent/skill_store.py`,
  `sre_agent/act_phase.py`, `sre_agent/graph_builder.py`,
  `sre_agent/agent_runtime.py`, `sre_agent/agent_runtime_tasks.py`,
  `sre_agent/supervisor.py`, `sre_agent/runbook_generator.py`,
  `tests/test_verified_learning.py`.

## Verification commands and latest results
- `/tmp/sre-a08-venv/bin/python -m pytest -q --disable-warnings
  tests/test_verified_learning.py tests/test_skill_store.py
  tests/test_act_phase.py tests/test_runbook_generator.py
  tests/test_act_integration.py`: 45 passed.
- A09 matrix and release-gate suite previously passed on `4319f8f`.

## Known blockers or risks
- No live MCP, restart/resume, chaos-oracle, calibrated autonomy, adversarial
  model run, A08 trace, or production A09 evidence bundle has been exercised.
- Alternate runner and follow-up closure paths still need a broader R05 sweep.
- Process-local dispatch can lose queued jobs; R02 remains open.

## Next bounded task
Finish A10 checkpoint: update canvas status, commit verified-only learning, then
either open stacked PRs for A02–A10 or start the next dependency-safe platform
item (for example R02 durable dispatch or R05 residual recovery drift).
