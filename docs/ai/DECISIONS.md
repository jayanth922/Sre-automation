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
