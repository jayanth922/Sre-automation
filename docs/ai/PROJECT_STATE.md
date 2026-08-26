# PROJECT_STATE.md

## Project objective
Harden Sentinel's P0 trust and safety findings T01–T10 plus the R01 durability
prerequisite. The verified plan is
`/Users/jayan/.claude/plans/fluttering-inventing-lerdorf.md`.

## Current milestone
Phase 3. **T01–T10, R01, and security-review fixes are implemented** on
`codex/p0-trust-safety-hardening`, pending publication as a GitHub PR.

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
- Agent graphs, tools, and MCP clients use immutable tenant `ExecutionContext`
  objects and non-secret per-cluster cache fingerprints. MCP routes and policy
  environment come only from operator configuration; tenant URLs/alert labels
  cannot receive the service token or weaken production policy.
- Cluster credentials use versioned AES-GCM storage. The connection token has a
  separate SHA-256 lookup hash; response schemas expose no stored credentials.
- API-to-MCP HTTP/SSE transport requires `MCP_SERVICE_TOKEN`; edge servers fail
  closed before FastMCP and local published ports bind to loopback.
- Non-autonomous reports create a PostgreSQL `ApprovalRequest` before a durable
  graph interrupt. Admin/org/hash/expiry checks and an atomic pending-row CAS
  precede synchronous resume on the stored thread; blocked actions stay blocked.
- Helm grants observer/actuator access only in the configured workload namespace
  by default. Cluster-wide roles are opt-in; only pods carry the delete verb.
- Every live write goes through `mutation_gateway.authorize_and_execute`, which
  freshly checks Redis lock state, policy, tenant namespace, and an atomic
  idempotency claim before the private executor core, then persists `AuditEvent`.

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
- T06+T09: per-cluster execution context/runtime cache, production fail-closed
  tool construction, encrypted credential migration/rotation, hashed cluster
  token lookup, authenticated MCP transport, and loopback edge ports;
  `tests/test_execution_context.py`, `tests/test_crypto.py`,
  `tests/test_credential_redaction.py`, `tests/test_mcp_auth.py`.
- T07: durable exact-action approvals, async PostgreSQL checkpoint resume,
  atomic expiry/replay protection, synchronous execution, and dashboard request
  wiring; `tests/test_approval_flow.py`.
- T10: namespaced observer/actuator Roles, pod-only deletion, explicit
  cluster-wide opt-in, Helm CI and live authorization checks;
  `tests/test_rbac_scope.py`.
- T08: sole live-mutation gateway, fresh lock/policy/scope authorization, Redis
  `SET NX EX` idempotency, private unchecked executor, and canonical audit
  persistence; `tests/test_mutation_gateway.py`.
- Security review fixes: operator-only MCP routing, trusted policy environment,
  internal-token auth on the legacy webhook, and fail-closed structured MCP
  outcomes with honest audit/verification status.

## Active problem
The complete hardening stack is ready for GitHub review.

## Relevant files
- Review fixes: `execution_context.py`, `executor.py`, `act_phase.py`,
  `agent_runtime.py`, `mutation_gateway.py`, and focused tests.

## Verification commands and latest results
Scratch Python environment:
`/Users/jayan/.claude/jobs/ffa758d9/tmp/venv/bin/python`.

- Focused T01–T10/R01 security, policy, and executor suite: 231 passed, 3
  dependency-based skips (LangGraph is absent from the scratch environment).
- Independent security review suite: 102 passed, 1 dependency-based skip.

## Known blockers or risks
- No complete project dependency environment is installed, so no live FastAPI,
  dashboard, browser, MCP, PostgreSQL migration, or real T07 restart/resume test
  ran; affected tasks use pure tests and bounded source contracts.
- Helm is not installed in the scratch environment, so chart lint/render could
  not run; a CI render assertion was added. Live `kubectl auth can-i` is also
  blocked without a reachable test cluster.
- The dashboard's default WebSocket proxy path mismatch is a separate P03 item;
  deployments must currently set `NEXT_PUBLIC_WS_BASE` correctly.
- The dependency stack remains intentionally combined for one full review.

## Next bounded task
Publish the review branch and open its PR; do not merge without user approval.
