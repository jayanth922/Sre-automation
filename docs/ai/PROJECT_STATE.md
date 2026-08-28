# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. T01–T10, R01, security fixes, and P03 routing are merged to `master`
through PRs #1 and #2. A01 immutable run provenance is open in PR #3; it is
mergeable and all four GitHub checks pass. A02–A07 are stacked local branches
on A01 and still need remote branches and reviewable PRs. A07 adversarial
evaluation is implementation-complete on `codex/a07-adversarial-eval`.

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
  grades, statistical gates, confidence calibration, and adversarial gates fail
  closed when evidence is absent, incomparable, or uncalibrated.
- Alerts, logs, tool results, runbooks, repository text, retrieved memory, and
  prior model output are untrusted evidence, never approval or authority.

## Completed or verified work
- T01–T10/R01: truthful status, scoped auth/WebSockets/resources, secure
  invitations, encrypted tenant-bound tools, durable approvals, namespace RBAC,
  and the sole live-mutation gateway.
- P03: same-origin dashboard WebSockets and Helm ingress routing.
- A01: content-pinned run manifests, comparison APIs, source SHA, and migration
  enforcement.
- A02: independent baseline/failure/consecutive-recovery oracle and append-only
  evidence.
- A03: strict content-addressed train/dev/holdout scenarios plus reversible
  Meridian fault adapter.
- A04: pinned typed grading and fail-closed human-label calibration contracts.
- A05: paired randomized trials, confidence intervals, risk slices, and
  non-inferiority/safety gates.
- A06: reliability metrics, monotonic calibration artifacts, drift checks, and
  last-mile calibrated autonomy enforcement.
- A07: content-addressed injection/forged-approval/malicious-runbook/tool-spoof/
  exfiltration/cross-tenant cases; strict zero-tolerance evaluator; evidence
  envelopes, redaction, and prompt policies across planner, reflector,
  specialist, narrative, and aggregation paths.

## Active problem
A02/A03 still need a live chaos-backed Meridian run. A04 has no adjudicated
human-label set or measured judge agreement. A05 has no paired live artifact and
cannot promote while A04 grades or A08 costs are incomplete. A06 has no real
calibration/drift artifact, so mutations require approval. A07 has no live or
replayed model observation artifact; synthetic test observations cannot support
a release claim.

## Relevant files
- A01: `sre_agent/run_manifest.py`, database model/migration, runtime/jobs API.
- A02–A06: `benchmarks/recovery_oracle.py`, `scenario_dataset.py`,
  `structured_grading.py`, `statistical_eval.py`, `confidence_eval.py`, and
  `sre_agent/confidence_calibration.py`.
- A07: `sre_agent/prompt_guard.py`, prompt/graph/narrative/supervisor wiring,
  `benchmarks/adversarial_eval.py`, `benchmarks/adversarial/v1/`, and the two
  A07 test files.

## Verification commands and latest results
- `/private/tmp/sentinel-a01-venv/bin/python -m pytest -q`: 491 passed, 10
  Pydantic deprecation warnings.
- A02–A07 focused evaluator suite: 84 passed.
- A07 focused suite: 13 passed; new evaluator/guard files pass Black and Ruff;
  changed Python compiles and `git diff --check` passes.

## Known blockers or risks
- No live MCP, restart/resume, cluster authorization, chaos-oracle, calibrated
  autonomy, or adversarial-model run has been exercised.
- A01's PostgreSQL migration has only run in CI.
- The holdout is CI-protected but public; a truly hidden evaluator is future work.
- Process-local dispatch can lose queued jobs; R02 remains open.
- `agent_runtime_tasks.py`, follow-up closure, and pre-verification learning still
  contain non-canonical recovery drift for R05/A10.

## Next bounded task
Implement A08 trace-complete model accounting: record actual routed model and
fallback per call, token usage, cost, latency, trace linkage, and explicit
completeness reasons; make evaluation/cost claims fail closed when any call is
unreconciled.
