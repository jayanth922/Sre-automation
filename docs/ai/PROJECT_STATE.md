# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. **T01–T10, R01, security-review fixes, and P03 routing are merged to
`master`** through [PR #1](https://github.com/jayanth922/Sre-automation/pull/1)
and [PR #2](https://github.com/jayanth922/Sre-automation/pull/2). A01 immutable
run provenance is implemented on `codex/a01-run-manifest`.

## Current architecture and invariants
- `sre_agent/incident_status.py::compute_incident_status` is the sole decision
  point for RESOLVED; graph completion alone does not mean recovery.
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

## Active problem
A01 awaits review and full CI on `codex/a01-run-manifest`. P03's production
dashboard build, readiness, and authenticated browser smoke criteria remain
separate work.

## Relevant files
- A01: `sre_agent/run_manifest.py`, `backend/models.py`, the run-manifest
  migration, runtime/jobs API wiring, and `tests/test_run_manifest.py`.

## Verification commands and latest results
Scratch Python environment:
`/Users/jayan/.claude/jobs/ffa758d9/tmp/venv/bin/python`.

- Focused T01–T10/R01 security, policy, and executor suite: 231 passed, 3
  dependency-based skips (LangGraph is absent from the scratch environment).
- Independent security review suite: 102 passed, 1 dependency-based skip.
- P03: 12 WebSocket tests and dashboard type-check passed locally; GitHub CI
  passed backend, frontend, rendered Helm routing/RBAC, and image builds.
- A01 focused provenance/model/auth/IDOR suite: 75 passed; Python compile and
  migration-head checks passed. CI now runs live `alembic upgrade head`.

## Known blockers or risks
- No live MCP, T07 restart/resume, or cluster authorization test has run.
- A01 records configured model routes and trace correlation; reconciling actual
  per-call fallback, tokens, cost, and trace completeness remains A08.
- Process-local job dispatch can still lose queued work before a worker starts;
  durable queue/worker execution remains R02.

## Next bounded task
Publish A01 for review, then implement A02's independent outcome oracle without
using graph completion or self-authored summaries as recovery evidence.
