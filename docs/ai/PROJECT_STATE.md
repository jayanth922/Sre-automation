# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. T01–T10, R01, security fixes, and P03 routing are merged to `master`
through PRs #1 and #2. A01 immutable run provenance is open in PR #3; it is
mergeable and all four GitHub checks pass. A02–A08 are stacked local branches
on A01 and still need remote branches and reviewable PRs. A08 trace-complete
accounting is implementation-complete on `codex/a08-trace-accounting`.

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
- A08: one root incident trace with required model, retrieval, tool, policy,
  approval, mutation, and verification spans; actual model/fallback, provider
  usage/cost, latency, errors, and A01 correlation; metadata-only append-only
  artifacts; opt-in redacted payloads; complete-trace cost gates in trial v2.

## Active problem
A02/A03 still need a live chaos-backed Meridian run. A04 has no adjudicated
human-label set or measured judge agreement. A05 has no paired live artifact and
cannot promote while A04 grades or A08 traces are incomplete. A06 has no real
calibration/drift artifact, so mutations require approval. A07 has no live or
replayed model observation artifact. A08 has no live trace; providers that do
not report call cost deliberately leave cost and promotion evidence incomplete.

## Relevant files
- A01: `sre_agent/run_manifest.py`, database model/migration, runtime/jobs API.
- A02–A06: `benchmarks/recovery_oracle.py`, `scenario_dataset.py`,
  `structured_grading.py`, `statistical_eval.py`, `confidence_eval.py`, and
  `sre_agent/confidence_calibration.py`.
- A07: `sre_agent/prompt_guard.py`, prompt/graph/narrative/supervisor wiring,
  `benchmarks/adversarial_eval.py`, `benchmarks/adversarial/v1/`, and the two
  A07 test files.
- A08: `sre_agent/model_accounting.py`, `sre_agent/trace_evidence.py`, runtime,
  model-router/checkpointer/graph/API wiring, `benchmarks/accounting/`, and
  paired evaluation trial schema v2.

## Verification commands and latest results
- `/tmp/sre-a08-venv/bin/python -m pytest -q`: 508 passed, 11 existing
  dependency/Pydantic deprecation warnings.
- A08 integration suite: 91 passed across trace privacy/completeness, routing,
  evaluation, provenance, checkpointer, ACT, approval, and mission control.
- New/changed accounting files pass Black and Ruff; changed Python compiles and
  `git diff --check` passes.

## Known blockers or risks
- No live MCP, restart/resume, cluster authorization, chaos-oracle, calibrated
  autonomy, adversarial-model run, or A08 trace has been exercised.
- A01's PostgreSQL migration has only run in CI.
- The holdout is CI-protected but public; a truly hidden evaluator is future work.
- Process-local dispatch can lose queued jobs; R02 remains open.
- `agent_runtime_tasks.py`, follow-up closure, and pre-verification learning still
  contain non-canonical recovery drift for R05/A10.

## Next bounded task
Implement A09 release evaluation gating: run a versioned candidate/baseline
matrix in CI; freeze evidence artifacts; enforce zero-tolerance safety plus
approved quality, cost, and latency deltas; and define shadow/canary rollback.
A deliberately regressive prompt, model, or tool change must fail CI, and every
promoted configuration must have a pinned evidence artifact.
