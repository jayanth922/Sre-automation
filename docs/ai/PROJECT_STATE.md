# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. **T01–T10, R01, security-review fixes, and P03 routing are merged to
`master`** through [PR #1](https://github.com/jayanth922/Sre-automation/pull/1)
and [PR #2](https://github.com/jayanth922/Sre-automation/pull/2). A01 immutable
run provenance is review-ready in
[PR #3](https://github.com/jayanth922/Sre-automation/pull/3), which is mergeable
with all four CI checks green. A02 independent recovery-oracle work is in the
committed `a734f01` stacked branch. A03 is committed as `1a4edda`. A04
is committed as `bc22c13`. A05 statistical-evaluation work is on
`codex/a05-statistical-eval`, stacked on A04.

## Current architecture and invariants
- In the canonical API runner, `compute_incident_status` in
  `sre_agent/incident_status.py` is the sole RESOLVED decision point; graph
  completion alone does not mean recovery.
- Canonical user auth is in `sre_agent/api/v1/auth_deps.py`. Authenticated v1
  routers use router-level `get_current_user_and_org`; legacy global runtime
  routes require `INTERNAL_API_TOKEN` and fail closed when it is unset.
- Runtime graph construction uses `get_checkpointer()`, not a hardcoded
  `MemorySaver`.
- WebSockets use fresh 45-second scoped tickets, verify incident ownership
  before acceptance, filter global feeds by organization, and drop unscoped events.
- Helm rejects empty or placeholder session/WebSocket signing secrets.
- Central async dependencies scope cluster, incident, and SLO UUID queries to
  the caller's organization and return the same 404 for missing/cross-org IDs.
- Self-registration creates a new organization; joining one requires a hashed,
  single-use invitation whose persisted scope and role are authoritative.
- Canonical `AuditEvent` records may be cluster-scoped or organization-scoped,
  with a database check requiring at least one scope.
- Graphs, tools, and MCP clients use immutable tenant `ExecutionContext` objects;
  MCP routes and policy environment are operator-controlled.
- Credentials use versioned AES-GCM storage and hashed token lookup. API-to-MCP
  transport is authenticated; local edge ports bind to loopback.
- Non-autonomous reports create a PostgreSQL `ApprovalRequest` before a durable
  graph interrupt. Admin/org/hash/expiry checks and an atomic pending-row CAS
  precede synchronous resume on the stored thread; blocked actions stay blocked.
- Helm grants observer/actuator access only in the configured workload namespace
  by default. Cluster-wide roles are opt-in; only pods carry the delete verb.
- Every live write goes through `mutation_gateway.authorize_and_execute`, which
  freshly checks Redis lock state, policy, tenant namespace, and an atomic
  idempotency claim before the private executor core, then persists `AuditEvent`.
- Every started incident job writes one database-enforced immutable run manifest
  before graph invocation. Missing code/model/prompt/tool/tenant provenance marks
  the run non-comparable; tenant-scoped APIs expose and compare manifests.

## Completed or verified work
- T01: expanded incident outcome states and truthful status computation;
  `tests/test_incident_status.py`.
- R01: configured checkpointer used by `initialize_agent()`;
  `tests/test_checkpointer.py` source assertion.
- T02: router-level auth on user-facing APIs, internal-token protection for
  legacy globals, divergent `backend/rbac.py` removed;
  `tests/test_route_auth_coverage.py`.
- T03: ticket minting/validation, dashboard ticket acquisition, incident-org
  authorization, fail-closed feed filtering, stable Helm signing secret;
  `tests/test_ws_auth.py`.
- T04: centralized owned-resource dependencies and route wiring for cluster,
  mission-control, audit, and SLO operations; two-organization IDOR matrix in
  `tests/test_idor.py`.
- T05: secure admin-created invitations, atomic public acceptance, server-fixed
  user scope/role, organization audit events, and removal of registration's
  organization-name auto-join; `tests/test_invitations.py`.
- T06+T09: tenant-bound tools, encrypted credentials, authenticated MCP, and
  loopback edge ports; execution-context/crypto/MCP tests.
- T07: durable exact-action approvals and atomic PostgreSQL resume;
  `tests/test_approval_flow.py`.
- T10: namespaced observer/actuator Roles, pod-only deletion, explicit
  cluster-wide opt-in, Helm CI and live authorization checks;
  `tests/test_rbac_scope.py`.
- T08: sole live-mutation gateway with fresh authorization, idempotency, and
  audit persistence; `tests/test_mutation_gateway.py`.
- Security review fixes: trusted MCP policy/routes and fail-closed outcomes.
- P03: dashboard defaults to same-origin `/ws`, Helm ingress routes that path
  directly to the API, and explicit split-origin overrides remain supported.
- A01: secret-free run manifests capture code/graph/prompt/model/tool/input/tenant
  provenance, root trace correlation, comparability reasons, and exact config
  drift; image builds carry their source SHA and CI applies migrations.
- A02 implementation: benchmark scenarios own deterministic Prometheus probes;
  recovery requires a healthy baseline, a failing observation, then consecutive
  healthy observations; ambiguous/missing evidence fails closed. Application
  status is comparison context only, false-resolved claims are explicit, and
  append-only JSONL evidence is stored outside incident/job output.
- A03 first slice: content-addressed v1 train/dev/frozen-holdout manifests carry
  versions, taxonomy, risk, provenance, expected evidence, allowed/forbidden
  actions, declarative fault operations, and recovery probes. A strict loader
  validates schema/digests, protects holdout from CI/default access, and feeds
  the live benchmark; evidence records dataset/scenario versions and split SHA.
  The Meridian adapter verifies healthy `/admin/config`, applies typed faults,
  confirms values, and restores the original snapshot in a `finally` path.
- A04 first slice: pinned `sre-structured-v1` schemas replace keyword diagnosis
  and action-type-only remediation credit with exact typed service/fault/action/
  target checks. Runtime summaries expose a dedicated structured evaluator
  payload; raw outputs, hashes, rubric versions, and judgments are stored
  together. Semantic causal/evidence criteria remain fail-closed pending blinded
  calibration. The label loader enforces opaque case IDs, independent raters,
  rationales, Cohen's kappa, and configurable release thresholds.
- A05 first slice: strict content-addressed trial records pin experiment,
  scenario, dataset, risk, candidate configuration, oracle/grader/safety
  outcomes, latency, cost coverage, failure taxonomy, and raw evidence paths.
  Candidate runs share a deterministic randomized schedule and pair IDs.
  Reports include per-scenario distributions, Wilson and paired bootstrap
  intervals, pass@k/pass^k, paired deltas/effect sizes, critical-risk slices,
  and fail-closed non-inferiority/uncertainty/safety/structured-grade gates.

## Active problem
A02/A03 need a live chaos-backed run. Automatic and manual fault modes now
exist, but service URL reachability, Prometheus label/query compatibility,
Alertmanager delivery, and cleanup have not run against Meridian. Only the four
existing evidence-backed scenarios were migrated; clean, noisy, multi-fault,
capacity, security, partial-outage, and no-action fixtures remain. P03's
production dashboard build/readiness/browser smoke remain separate work.
A04 has no real human-labeled calibration set, adjudicated labels, measured
judge agreement, or calibrated model judge. It therefore reports semantic
criteria as `REQUIRES_CALIBRATION` instead of quality scores.
A05 has no live paired baseline/candidate trial artifact. Cost remains unknown
until A08 supplies trace-complete accounting, and the promotion gate blocks
while A04 structured grades are incomplete.

## Relevant files
- A01: `sre_agent/run_manifest.py`, `backend/models.py`, the run-manifest
  migration, runtime/jobs API wiring, and `tests/test_run_manifest.py`.
- A02: `benchmarks/recovery_oracle.py`, `benchmarks/sre_bench.py`,
  `benchmarks/scoring.py`, `tests/test_recovery_oracle.py`, and
  `tests/test_bench_scoring.py`.
- A03: `benchmarks/scenario_dataset.py`, `benchmarks/fault_adapter.py`,
  `benchmarks/datasets/v1/`, `tests/test_scenario_dataset.py`, and
  `tests/test_fault_adapter.py`.
- A04: `benchmarks/structured_grading.py`,
  `benchmarks/grader_calibration.py`, `benchmarks/graders/v1/`,
  `tests/test_structured_grading.py`, and
  `tests/test_grader_calibration.py`.
- A05: `benchmarks/statistical_eval.py`, `benchmarks/evaluation/v1/`,
  `tests/test_statistical_eval.py`, and A05 recording in
  `benchmarks/sre_bench.py`.

## Verification commands and latest results
Scratch Python environment:
`/Users/jayan/.claude/jobs/ffa758d9/tmp/venv/bin/python`.

- Focused T01–T10/R01 security, policy, and executor suite: 231 passed, 3
  dependency-based skips (LangGraph is absent from the scratch environment).
- Independent security review suite: 102 passed, 1 dependency-based skip.
- P03: 12 WebSocket tests and dashboard type-check passed locally; GitHub CI
  passed backend, frontend, rendered Helm routing/RBAC, and image builds.
- A01: full local suite 415 passed; focused suite 75 passed; GitHub backend,
  frontend, manifests, and image-build checks pass.
- A02–A05: 62 focused tests pass; evaluator files pass Black/Ruff and all
  changed Python compiles. A new full-suite attempt could not collect five
  unrelated modules because the available scratch environment lacks project
  dependencies (`pydantic`, `PyYAML`, `langchain-core`, and `python-jose`).

## Known blockers or risks
- No live MCP, T07 restart/resume, or cluster authorization test has run.
- PR #3 has green CI, but its live PostgreSQL migration behavior has not been
  exercised outside GitHub's migration job.
- A01 records configured model routes and trace correlation; reconciling actual
  per-call fallback, tokens, cost, and trace completeness remains A08.
- Process-local job dispatch can still lose queued work before a worker starts;
  durable queue/worker execution remains R02.
- No live A02 oracle artifact exists yet; Prometheus label/query compatibility
  and real fault-to-recovery timing remain unverified. Local readiness checks
  found no listeners on Sentinel 8080, Prometheus 9090, or Meridian inventory
  8002, so an end-to-end run was not attempted.
- V1 holdout is frozen and CI-protected but stored in the public repository; a
  truly hidden external holdout and controlled evaluator remain future work.
- T01 has residual non-canonical drift: `agent_runtime_tasks.py` still writes
  RESOLVED unconditionally, the follow-up closure heuristic can treat any
  summary as closed, and Qdrant/skill learning can occur before objective
  verification. These belong to R05/A10 cleanup and must not be treated as
  canonical recovery evidence.

## Next bounded task
Run two live candidate configurations with the same A05 experiment/pair seed
and at least the policy minimum trials, then preserve the comparison report.
This remains blocked on the live Meridian/Sentinel stack and genuine A04
calibration labels; do not weaken either gate to manufacture promotion.
