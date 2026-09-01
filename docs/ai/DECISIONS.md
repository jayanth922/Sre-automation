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
  (see `runbooks_notion/server.py`'s existing precedent) — header name
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

## Runbooks migrated to Notion-only hosting

- **Decision:** Deleted the local markdown runbook corpus entirely
  (`edge_mcp_servers/mcp_servers/runbooks_local/`, `sre_agent/runbooks_corpus.py`)
  across all three consumers, not just the human-facing catalog API: (1)
  `sre_agent/api/v1/runbooks.py` now reads only via `sre_agent/notion_runbooks.py`,
  returning an empty list / 404 for a cluster with no Notion database
  configured, no local fallback; (2) the agent's live-investigation RAG tool
  is now served by `edge_mcp_servers/mcp_servers/runbooks_notion/server.py`
  (new, replaces `runbooks_local`), keeping the exact tool names/signatures
  (`search_runbooks` et al.) so `context_builder.py`/`graph_builder.py`'s
  by-name tool lookups needed no changes; (3) `sre_agent/runbook_generator.py`'s
  `write_runbook`/`write_runbook_generative` became `async` and publish via
  the new `notion_runbooks.upsert_notion_runbook` instead of writing a local
  `.md` file. Notion credentials extend the existing per-cluster relay
  pattern from "Per-cluster credentials relay over the MCP transport" above
  (new `X-Sentinel-Relay-Notion-{Key,Database}` headers, same
  relayed-over-static-env-var precedence, same edge-side bounded cache
  convention) rather than inventing a separate mechanism.
- **Reason:** User instruction: "regarding runbooks, remove local uploads of
  runbooks. only hosted on notion." Production teams already keep runbooks in
  Notion; a local file corpus was redundant, went stale independently of the
  source of truth, and — since `edge_mcp_servers/mcp_servers/*` images ship to
  customers — meant shipping a fixed example corpus baked into the container.
- **Consequences:** The Notion-backed MCP server does lexical-only search (no
  `fastembed`/embeddings), a deliberate simplification versus the old local
  server, since Notion's REST API has no cheap full-content search without
  per-page fetches. `upsert_notion_runbook` implements "upsert" as
  archive-then-create (Notion has no atomic replace-content call), which
  leaves a trash-recoverable archived page behind each time an auto-generated
  runbook's signature regenerates — acceptable given Notion's API limits. A
  cluster with no Notion database configured simply has no runbooks and no
  generative-runbook writes; this is a behavior change from the old
  local-corpus fallback, which always had *something* to serve. No schema is
  assumed on the Notion database beyond "there is a title property" —
  service/incident-type/severity are only set when the database has a
  same-named property.
- **Rejected alternative:** Keeping the local corpus as a fallback when Notion
  isn't configured — rejected because it directly contradicts the user's
  "only hosted on notion" instruction and would leave two divergent runbook
  sources to keep in sync.

## Anthropic extended-thinking `AIMessage.content` is normalized at the two consumer sites, not one shared helper

- **Decision:** `sre_agent/agent_nodes.py` (specialist message capture) and
  `sre_agent/supervisor.py` (final synthesis capture) each independently
  detect `isinstance(content, list)` and join only `type == "text"` blocks,
  rather than adding a single shared normalization utility.
- **Reason:** Both call sites are small, already-distinct extraction points
  (one per streamed chunk in a specialist's message loop, one on a single
  final LLM response), and `sre_agent/narrative.py::_invoke_llm()` already
  has its own independent (correct) version of this same normalization —
  three call sites already existed with this pattern in some form before this
  fix; consolidating now would touch more surface than the bug required.
- **Consequences:** Any *new* code path that reads `.content` off an
  Anthropic `AIMessage` and assumes `str` needs the same guard added by hand
  — this is a landmine that can recur. Search for `\.content\b` assignments
  from `AIMessage`/`response` objects before trusting `.strip()`/string ops
  on them.
- **Rejected alternative:** A shared `normalize_ai_content()` helper in
  `sre_agent/llm_utils.py` — deferred as unnecessary scope for a bug fix;
  worth doing if a fourth call site turns up.

## MCP server output capping applies to instant queries too, not just range queries

- **Decision:** Prometheus's `get_metric` (instant PromQL) and
  `get_golden_signals` now cap result size via `_cap_vector_result()`
  (`MAX_INSTANT_SERIES = 50`, then a byte-size fallback), mirroring the
  existing `_downsample_range_result()` cap on `get_metric_range`. Loki's
  `_cap_logs()` was fixed to handle the zero-result case (`while True:`
  instead of `while capped:`, which was falsy — and so skipped entirely — on
  an empty list).
- **Reason:** An under-filtered instant query can match thousands of series,
  each carrying a full label set; this caused the same class of LLM-context
  overflow the range-query cap was already built to prevent, plus a separate
  crash (`**None` from the empty-list case) that only manifested on queries
  legitimately returning zero results.
- **Consequences:** Any new Prometheus/Loki MCP tool that returns raw
  query results needs to run through one of these capping helpers before
  being handed to the LLM — this is now the established pattern for this
  codebase, not a one-off fix.

## Live-execution target parsing and MCP response classification must handle real Planner/SDK shapes, not just the tested subset (Task #16)

- **Decision:** `sre_agent/executor.py::_live_args()` extracts a k8s resource
  name from a Planner `target` string via a leading DNS-1123-label regex
  (`_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?")` on the
  lowercased target), not by splitting on `:`. Separately,
  `classify_live_response()`'s `_structured_payload()` now recurses into a
  `"text"` **key** (in addition to `"result"`/`"data"`/`"content"`) on any
  `Mapping`, because the MCP SDK's real content-block response is a list of
  plain dicts shaped `{"type":"text","text":"<json>","id":...}` — the
  JSON-encoded tool payload lives under that key, not under a `.text`
  *attribute* (the pre-existing `getattr(value, "text", None)` fallback only
  ever fires for SDK objects, never plain dicts, so it silently never ran).
- **Reason:** Both bugs were only found by validating a genuine end-to-end
  live run (Task #16) against real telemetry instead of trusting dry-run/
  mocked-response tests. Real Planner `target` strings are free-form English
  (e.g. `"checkout-service pods (targeted canary subset only, ...)"`), not
  the `"<deployment>:<sub-resource>"` shape the old colon-split fix assumed.
  And the `_structured_payload()` gap meant **every** live MCP tool response
  — genuine successes and legitimate policy refusals alike — fell through to
  `return None`, so `classify_live_response()` reported `"ERROR"`
  regardless of what actually happened; this had been silently
  misclassifying `patch_resource_limits` refusals as errors all along, not
  just restarts.
- **Consequences:** A third, smaller fix rides along: the WARNING log line
  in `_aexecute_unchecked()` previously dropped the `detail` string computed
  by `classify_live_response()`, making the true failure/refusal reason
  invisible in logs at any level — it's now appended to the log line. Any
  future MCP tool integration must be validated against the SDK's actual
  content-block wrapping (a list of `{"type":"text","text":...}` dicts), not
  an idealized flat-dict mock, or this exact class of false-ERROR
  misclassification will recur. Covered by 6 new unit tests in
  `tests/test_executor.py` using the exact captured real MCP payloads (full
  suite: 780 passed / 2 skipped).
- **Rejected alternative:** Trusting the dry-run test suite alone as
  sufficient validation for live execution — it could not have caught
  either bug, since both are shape mismatches between mocked test fixtures
  and the real Planner/MCP SDK's actual output.

## Cloud dev environments are populated via `rsync`, not `git push`

- **Decision:** When standing up a remote VM/Codespace to run this stack
  faster, the local working tree (including uncommitted changes) is copied
  over with `rsync -e ssh --exclude .git ...`, not by pushing a branch.
- **Reason:** Keeps in-progress, unreviewed work off GitHub and out of git
  history entirely, consistent with the standing "never commit/push without
  explicit instruction" policy — syncing files bypasses git, so it carries no
  such implication.
- **Consequences:** A remote environment set up this way has no relationship
  to `origin` until/unless something is later committed and pushed on
  purpose; its Postgres/Redis/etc. volumes start empty, so any incident data
  created locally (e.g. the regression-test incidents from this session) does
  not exist there unless separately dumped/restored.
