# Durable decisions

## Existing-organization membership requires an invitation

- **Decision:** Self-registration always creates a new organization and its
  founding admin. Joining an existing organization requires a hashed,
  single-use `OrgInvitation`; email, organization, and role are taken from that
  server-side record during acceptance.
- **Reason:** A caller-controlled organization name and role must never grant
  tenant membership or privileges.
- **Consequences:** Organization names need not be unique identifiers. Invitation
  acceptance is public but token-authenticated, row-locked, and audited.
- **Rejected alternative:** Automatic membership based on organization name or
  an unverified email domain.

## Canonical audit events support organization scope

- **Decision:** `AuditEvent` can reference either a cluster or an organization;
  a database constraint requires at least one scope.
- **Reason:** Invitation events occur before a user or organization necessarily
  has a cluster, so assigning them an arbitrary cluster would be inaccurate.
- **Consequences:** Existing cluster audit behavior remains valid, while
  organization-level security events use `organization_id` and `cluster_id=NULL`.

## Agent runtimes are cached per execution context

- **Decision:** Graphs, tools, and MCP clients are built from an immutable
  `ExecutionContext` and cached by cluster plus a non-secret context fingerprint.
  MCP transport endpoints and policy environment come only from operator-owned
  deployment configuration; tenant identity and namespace remain context-bound.
- **Reason:** Tenant-controlled endpoints could exfiltrate the shared MCP bearer
  token, while alert-controlled environment labels could bypass production rules.
- **Consequences:** Production refuses context-free construction and any MCP URL
  that differs from the exact configured service route. Context changes close the
  previous client; unknown environments fail to production.
- **Rejected alternative:** Tenant cluster URLs as MCP destinations, alert labels
  as policy environment, or one process-global graph/client.

## Cluster credentials use versioned authenticated encryption

- **Decision:** Cluster secrets use AES-GCM ciphertexts carrying their key
  version. Historical keys remain available through a versioned keyring during
  rotation; cluster connection tokens use a separate SHA-256 lookup hash.
- **Reason:** Nondeterministic encryption protects stored credentials but cannot
  support indexed token authentication directly.
- **Consequences:** `CREDENTIAL_ENCRYPTION_KEY` and `MCP_SERVICE_TOKEN` are
  required deployment secrets. MCP HTTP/SSE transport rejects missing or
  mismatched bearer tokens before invoking FastMCP.
- **Rejected alternative:** Plaintext storage, deterministic token encryption,
  or unauthenticated trust based solely on network location.

## Human approval authorizes one durable action hash

- **Decision:** A non-autonomous report is persisted as a PostgreSQL
  `ApprovalRequest` before LangGraph interrupts. Approval requires an org admin,
  the active interrupt's exact action hash, an unexpired pending row, and an
  atomic single-use transition before synchronous resume on its stored thread.
- **Reason:** Process memory, client-supplied action data, or fire-and-forget
  resume cannot safely survive restarts or prevent tampering and replay.
- **Consequences:** API deployments require an async external checkpointer and
  fail closed if it is unavailable; Helm defaults to PostgreSQL checkpointing.
  Hard-blocked actions remain blocked even after human approval.
- **Rejected alternative:** Redis-only pending state, in-memory graph globals,
  unverified resume strings, or asynchronous background resume.

## Kubernetes mutation privileges are namespaced by default

- **Decision:** Observer and actuator ServiceAccounts receive namespaced Helm
  Roles in the configured workload namespace. Cross-namespace ClusterRoles are
  rendered only when `rbac.clusterWide.enabled=true`; delete applies only to pods.
- **Reason:** A compromised tool must not inherit cluster-wide mutation or
  deletion rights unrelated to its registered workload scope.
- **Consequences:** The ServiceAccounts stay in the Sentinel release namespace
  and are referenced by RoleBindings in the workload namespace. Operators must
  explicitly opt into cluster-wide access.
- **Rejected alternative:** Default ClusterRoleBindings or a combined delete
  rule covering pods, services, nodes, events, and namespaces.

## Incident memory uses named vectors and deterministic point IDs

- **Decision:** `memory_store.py` embeds symptoms/root_cause/resolution as
  three separate named vectors per point (not one flat blob), in a renamed
  `sre_incidents_v2` collection, with Qdrant point IDs derived via
  `uuid.uuid5(NAMESPACE, incident_id)` instead of Python's built-in `hash()`.
- **Reason:** A single flat embedding blurs distinct signals a query might
  match on (what was observed vs. why vs. how it was fixed). Separately,
  `hash()` on strings is `PYTHONHASHSEED`-randomized per process, so the old
  `hash(incident_id) % (2**63)` scheme produced a different Qdrant point ID
  for the same `incident_id` across process restarts — silently duplicating
  points instead of upserting the existing one, and making it impossible to
  reliably look up a point by `incident_id` for cross-incident back-linking.
- **Consequences:** The old `sre_incidents` collection (single flat vector)
  is abandoned in place, not migrated — Qdrant is local/self-hosted per
  `platform/docker-compose.yaml` with no production tenant traffic yet,
  so there is no data worth migrating. Any future code that needs a point
  for a known `incident_id` must use `memory_store._point_id()`, not query by
  payload filter.
- **Rejected alternative:** Reusing the `sre_incidents` collection name with
  changed `vectors_config` (Qdrant would reject upserts against the
  pre-existing incompatible schema), or a migration script for a collection
  with no real tenant data to preserve.

## Live writes cross one fresh authorization boundary

- **Decision:** `mutation_gateway.authorize_and_execute` is the only application
  caller of the executor's private live core. It freshly reads the cluster lock,
  reruns policy, verifies tenant/namespace scope, and atomically claims a Redis
  idempotency key before the tool call; its hashed executor audit is persisted as
  the canonical `AuditEvent` afterward.
- **Reason:** A plan or approval can become stale before execution, while process-
  local duplicate suppression cannot protect retries across workers or restarts.
- **Consequences:** Redis and tenant context are required for live mutation.
  Duplicate claims short-circuit without another tool call; an audit failure is
  surfaced while the claim remains active to prevent an unsafe retry.
- **Rejected alternative:** Calling `Executor` from ACT with a cached gate result
  or checking idempotency with separate read and write operations.

## Per-cluster credentials relay over the MCP transport (Phase 4)

- **Decision:** `edge_mcp_servers/*` keep resolving credentials from static
  process environment variables (`GITHUB_TOKEN`/`GITHUB_REPO`, `KUBECONFIG`)
  as their fallback, but a request-scoped credential relay now takes
  priority: `sre_agent/multitenant/relay_auth.py::build_relay_headers`
  attaches one cluster's resolved GitHub/K8s credentials as additional
  `X-Sentinel-Relay-*` headers on the MCP connection built fresh per
  investigation (`build_mcp_server_config`); the edge-side ASGI bearer-auth
  middleware (`mcp_auth.py`) captures them into a `contextvars.ContextVar`
  (`edge_mcp_servers/relay_credentials.py`), and only two choke points read
  them back — `github_real/server.py::_active_repo()` and
  `k8s_real/server.py::_relay_api_client()` — both bounded caches (max 8
  entries) keyed by the credential itself.
- **Reason:** One `Organization` can own many `Cluster` rows, but the edge
  fleet's static single-tenant env vars were only ever correct for exactly
  one `Cluster` per deployment. Rewriting every tool handler to thread a
  credential parameter through would touch far more surface area than the
  two functions that actually gate all GitHub/K8s tool calls.
- **Consequences:** `edge_mcp_servers` still must never import `sre_agent`
  (see `runbooks_local/server.py`'s existing precedent) — header name
  constants are duplicated as plain strings on both sides and must be kept
  in sync by hand. GitHub tokens now also come from a GitHub App
  installation (`sre_agent/multitenant/github_app.py`) when
  `Cluster.github_app_installation_id` is set, minting a short-lived (~1h)
  token per resolution instead of relaying the long-lived stored PAT;
  minting failures fall back to the stored PAT non-fatally, same convention
  as `sre_agent/integrations/jira.py`. Slack similarly moves from a single
  global `SLACK_BOT_TOKEN` to a per-`Organization` OAuth-installed token
  (`sre_agent/multitenant/slack_oauth.py`, `Organization.slack_bot_token`),
  with the env var kept as the self-hosted fallback.
- **Rejected alternative:** A large mechanical rewrite threading a
  `Cluster`/credentials object through every MCP tool handler signature, or
  giving each `Cluster` its own edge deployment (defeats the point of a
  shared control plane and multiplies operational cost per tenant).

## AIOpsLab adapter plays back one investigation, not a live shell loop (Phase 5)

- **Decision:** `benchmarks/aiopslab_adapter.py` does not reuse
  `sre_bench.py`'s fire-webhook/poll-oracle harness pattern, even though that
  was the plan's original sketch for this phase. Reading the live package
  (github.com/microsoft/AIOpsLab: `orchestrator.py`, `parser.py`, the four
  `tasks/*.py` files) showed AIOpsLab is an in-process orchestrator that owns
  fault injection/workload/eval itself and drives a registered agent
  turn-by-turn via `agent.get_action(state: str) -> str`, parsing one
  markdown-fenced Python-call action per turn (`exec_shell(...)` /
  `submit(...)`). `SREAIOpsLabAgent.get_action` runs our pipeline once on the
  first turn, then plays back a fixed queue of AIOpsLab action strings built
  from that single investigation — for the mitigation task, one
  `exec_shell(...)` per already-executed remediation command (replaying
  `sre_agent/executor.py::build_command()`'s output verbatim), then the
  task-appropriate `submit(...)` (shape differs per task: detection/
  localization/analysis/mitigation each have a distinct `submit()` payload).
- **Reason:** Our pipeline investigates and remediates as one shot against
  its own MCP-tool surface; it is not a turn-by-turn shell-driving agent the
  way AIOpsLab's reference GPT client is. Building a true step-by-step
  `exec_shell` explorer that reasons live off AIOpsLab's own tool output
  would mean re-implementing investigation logic against a second, unrelated
  tool surface — out of scope for a benchmark adapter.
- **Consequences:** AIOpsLab's own `eval()` grades the *submitted* answer
  (and, for mitigation, post-`exec_shell` cluster state) against its ground
  truth and returns `TTD`/`TTL`/`TTA`/`TTM` plus accuracy fields —
  `from_aiopslab_run()` only normalizes that dict for reporting; it does not
  score independently the way `scoring.py::score_run` does for our own
  harness. A live run needs `aiopslab` installed (not a project dependency,
  same as `terminal-bench`) plus a local kind/minikube cluster with Helm —
  `run_problem()` raises a clear `RuntimeError` when the package is missing;
  `aiopslab_available()` lets callers check first.
- **Rejected alternative:** Forcing the `sre_bench.py` webhook+oracle shape
  onto AIOpsLab by wrapping its cluster behind our own alert-webhook API —
  not possible without forking AIOpsLab's orchestrator, which owns the
  cluster lifecycle end-to-end and never exposes an HTTP surface to fire
  synthetic alerts at.
