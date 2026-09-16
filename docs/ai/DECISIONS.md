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
  in sync by hand. GitHub credentials are relayed as the stored PAT
  (`Cluster.github_token`) only — the GitHub App installation-token flow
  (`sre_agent/multitenant/github_app.py`) was removed 2026-09-08 since the
  platform authenticates to GitHub with a repo URL + PAT exclusively;
  `Cluster.github_app_installation_id` (DB column) is now unused dead
  storage, kept only to avoid an extra migration. Slack similarly moves from a single
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

## Correlation adjacency source: infer from k8s labels

- **Decision:** `sre_agent/incident_correlation.py::correlate`'s optional
  `adjacency` map (service -> neighbor services, used for the
  service-topology signal) will be populated by inferring a
  service-dependency graph from existing Kubernetes metadata already
  present in-cluster (Service/Deployment labels, Ingress/NetworkPolicy
  references, or an existing service mesh's topology if one is deployed) —
  not a hand-maintained config file, not an external APM/observability
  topology source.
- **Reason:** User's explicit choice among four options (k8s-label
  inference, manual adjacency config, defer/keep same-service fallback,
  external topology source) when the decision was raised — no new
  infrastructure required, and cluster manifests are assumed to already
  encode enough dependency information to be useful.
- **Consequences:** Implemented 2026-09-03 in `sre_agent/service_topology.py`
  (`build_adjacency_map` + `get_adjacency_map`), wired into
  `_record_correlation_shadow` in `sre_agent/api/v1/alerts.py`. Uses
  `app.kubernetes.io/part-of` label grouping plus a NetworkPolicy
  ingress/egress graph walker resolved via label-selector matching against
  Service selectors (new `list_network_policies` tool on the `k8s` MCP
  server). Cached per cluster for 5 minutes via
  `RedisStateStore.set_topology_cache`/`get_topology_cache`; any fetch
  failure degrades non-fatally to no adjacency signal (same as before this
  feature). Pure-function unit tested (`tests/test_service_topology.py`) and
  live-fire validated 2026-09-03 against the Codespace's `kind-meridian`
  cluster, including both the no-signal path and (via temporary test
  `part-of` labels, removed afterward) the positive-signal path — see
  `docs/ai/PROJECT_STATE.md`'s `service_topology.py` entry for detail.
  Validation surfaced and fixed one real bug: the raw MCP tool-caller
  response is a list of content blocks, not a bare string/dict, which the
  original `_parsed()` helper didn't unwrap (would have silently produced
  `adjacency=None` in production despite successful k8s calls). Accuracy for
  a given cluster still depends on how consistently that cluster's manifests
  actually encode `part-of` labels and NetworkPolicies — `kind-meridian`
  itself has neither by default, so this signal is currently a no-op there
  outside of test mutations.
- **Rejected alternatives:** Manual adjacency config (accurate but needs
  ongoing upkeep as the architecture changes); deferring entirely (keeps
  zero new work but leaves a known-weaker signal in place); external
  topology source (no existing APM/mesh topology source is deployed in this
  environment to pull from).

## Hermes safety review (`AGENT_RUNTIME=hermes`)

- **Decision:** `AGENT_RUNTIME=hermes` (Nous Research's Hermes Agent as the
  autonomous actor in `sre_agent/actor_runtime.py::HermesRuntime`) remains
  **not safe to enable in production** as of this review (2026-09-03).
  `hermes-agent` has never been installed in any environment this project has
  touched, so this review is static: reading `HermesRuntime`'s code plus
  Nous's own published docs (https://hermes-agent.nousresearch.com/docs/guides/python-library),
  not an actual run.
- **Reason / findings:**
  1. **No filesystem sandbox exists.** The documented `AIAgent` constructor
     has no `workdir` (or any) sandbox parameter — confirmed via the docs,
     not just inferred. `HermesRuntime._build_agent()` previously masked this
     by catching the resulting `TypeError` and silently falling back to an
     *unconfined* run; fixed to fail closed (raise) instead, since
     `generate_patch_activity` invokes this actor against a real cloned
     copy of `GITHUB_REPO` (`jayanth922/meridian-shop` in this project),
     fully autonomously, between gate 1 approval and gate 2.
  2. **Cross-tenant memory-leak risk via `task_id`.** Docs describe `task_id`
     as hermes-agent's memory-isolation key ("VM isolation"), but the only
     real call site (`generate_patch_activity`) passed no `task_id`, so every
     incident across every org/cluster shared the constructor's hardcoded
     default (`"sre-actor"`) while `skip_memory=False` ("self-improving"
     memory loop) stayed on — meaning one tenant's incident context could
     leak into another's remediation run. Fixed: `generate_patch_activity`
     now passes `task_id=f"sre-actor-{organization_id}-{incident_id}"`;
     `get_agent_runtime()` takes `task_id` as an explicit factory kwarg
     (dropped for the `local` backend, which has no such concept) so it
     isn't accidentally blindly forwarded to `LocalTerminalRuntime`.
  3. **No safe toolset allowlist can be set without installing the package.**
     Nous's docs list `enabled_toolsets`/`disabled_toolsets` params but do
     not enumerate valid toolset names beyond "web"/"terminal"/"browser"
     mentioned in passing — insufficient to build a real allowlist. Today's
     code passes `disabled_toolsets=None` (no restriction), so if enabled,
     Hermes would run with whatever tools ship by default, unconstrained.
     Not fixed — needs either installing+introspecting the real package or
     upstream doc clarification.
  4. `max_iterations` default here (20) is already well under the package's
     documented default (500) — no action needed, noted as a mitigating
     factor already in place.
- **Consequences:** Items 1 and 2 fixed in `sre_agent/actor_runtime.py` /
  `sre_agent/incident_remediation_workflow.py` (tests added in
  `tests/test_actor_runtime.py`), independent of whether `hermes-agent` is
  ever installed — both are correct regardless of the package's actual
  internals. Item 3 remains open and is the actual blocker on enabling
  `AGENT_RUNTIME=hermes`: before flipping that env var anywhere real,
  either (a) install `hermes-agent` in a disposable environment and
  introspect its real toolset names to build an explicit `enabled_toolsets`
  allowlist, or (b) run `HermesRuntime` inside the project's existing
  ephemeral-K8s-Job sandbox infra (`edge_mcp_servers/mcp_servers/sandbox_real/`)
  instead of trusting any in-process confinement at all — the latter is the
  architecturally stronger fix but is a bigger change, not done here.
  Phase E cutover should treat this as still blocked, not cleared, by this
  review.
- **Rejected alternatives:** Installing/dry-running `hermes-agent` directly
  to answer the toolset-enumeration question — deferred rather than done
  unilaterally, since it means pulling and executing an "autonomous,
  tool-using" third-party agent framework of unknown tool surface, which is
  exactly the class of action this review flags as needing authorization
  first.

## Hermes removal — drop the pluggable actor backend, keep only `LocalTerminalRuntime`

- **Decision (2026-09-03):** Fully remove `HermesRuntime` and
  `AGENT_RUNTIME=local|hermes` backend selection from
  `sre_agent/actor_runtime.py`. `get_agent_runtime()` now unconditionally
  returns `LocalTerminalRuntime` — the actor is no longer pluggable, since
  there is only one implementation. `hermes-agent` is dropped from
  `pyproject.toml`'s optional extras and `uv.lock`; the Hermes-specific
  tests in `tests/test_actor_runtime.py` are deleted; the `task_id=` kwarg
  (Hermes's memory-isolation key) is removed from
  `incident_remediation_workflow.py`'s `generate_patch_activity` call site.
  `sre_agent/skill_store.py` (the self-improving skill-memory module,
  originally credited to Hermes's "save every workflow as a skill" feature
  in its docstring) is unaffected in behavior — it was always first-party,
  backend-agnostic code with no dependency on `HermesRuntime`; only its
  docstring's framing was updated.
- **Reason:** `AGENT_RUNTIME` defaults to `local` and Hermes was never
  actually selected in any real deployment this project has touched — it
  existed as an optional, never-installed alternative. The preceding safety
  review (see "Hermes safety review" above) already found it added risk
  (no filesystem sandbox, undocumented toolset surface) with the toolset gap
  still unresolved and blocking. When asked directly whether Hermes is
  architecturally necessary versus the existing deterministic actor, the
  answer is no: `LocalTerminalRuntime` already does the job the actor is
  responsible for (bounded, tool-using execution of a task handed to it by
  the Temporal-orchestrated `IncidentRemediationWorkflow`), is first-party,
  has zero extra dependencies, and has been live-fire validated end-to-end
  against the real cluster/repo. A third-party autonomous-agent framework
  at that boundary adds attack surface and an unresolved safety gap without
  adding any capability the deployment actually uses. User's own framing:
  "we can manually create a deterministic agent for temporal rather than
  hermes" — `LocalTerminalRuntime`/`TerminalAgent` already is that agent.
- **Consequences:** Simpler, single-implementation actor runtime — no env-var
  backend selection to reason about or keep safe. The Hermes safety review
  above is retained as historical record (not deleted, per this project's
  append-only decision-log convention) even though its subject no longer
  exists in the codebase. `docs/PROJECT_CONTEXT.md`, `docs/INTEGRATION_PLAN.md`,
  `docs/COMPETITIVE_AUDIT.md`, and `docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`
  contain historical narrative describing the Hermes integration as it was
  built and reviewed; where those docs asserted Hermes was actively in use
  or an open decision, they were annotated as superseded by this entry
  rather than rewritten wholesale.
- **Rejected alternative:** Keep `HermesRuntime` in place, unused but
  available behind the env var, in case a future need for an autonomous
  third-party actor arises. Rejected because unused, unreviewed-to-safety
  optional code paths are exactly the kind of latent risk this session's
  own process already flagged (proceeding on a stale "blocker" framing
  instead of first checking whether the code path was needed at all) —
  dead optionality that nobody will re-review before flipping on is worse
  than no optionality.

## Phase E cutover: code-fix actions always defer to the deterministic pipeline

- **Decision:** In `_act_gate_node` (`sre_agent/graph_builder.py`), detecting
  a code-fix action (`action_type` in `GITHUB_EXEC_TOOL_MAP | {"code_fix"}`)
  unconditionally marks its `decision` as `DEFERRED_TO_DETERMINISTIC_PIPELINE`,
  keeping it out of the old single-gate `execute_autonomous_live()` path.
  Whether `IncidentRemediationWorkflow` (the deterministic, two-gate pipeline)
  actually *starts* for that action is a separate, independent check — the
  existing "Code-fix verification" block, gated by `temporal_enabled()` and
  `_sandbox_params_ready(...)`, reports `INCONCLUSIVE` instead when unready.
- **Reason:** The prior code only deferred when Temporal was enabled *and*
  sandbox params were ready — i.e. the same readiness check gated both
  "should this start the deterministic pipeline" and "should this be allowed
  to run on the old live path", conflating two different questions. Any
  code-fix action detected while Temporal was disabled, or before
  `generate_patch_activity` had produced sandbox params, fell through
  unguarded to `execute_autonomous_live()` — bypassing Phase 5's two
  mandatory human approval gates entirely. This directly contradicted the
  user's original Phase 5 requirement (`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`):
  code changes must always go through the deterministic, Temporal-orchestrated
  pipeline with human gates at start-fix and raise-PR, never auto-execute.
- **Consequences:** A code-fix action detected while the deterministic
  pipeline isn't ready to start is now dropped from *both* paths for that
  cycle (deferred, but not picked up) rather than silently falling back to
  live auto-execution — the safer failure mode, matching "no default-approve
  path" from the Phase 5 plan. Non-code-fix actions (restart/scale/rollback/
  config_change/escalate/revert_commit) are unaffected and correctly remain
  on the old live path, since Phase 5's scope is code-fix/GitHub actions
  only. The import of `DEFERRED_TO_DETERMINISTIC_PIPELINE` from
  `incident_remediation_workflow.py` (which imports the optional `temporalio`
  SDK at module level) is wrapped in `try/except ImportError` with a
  hardcoded string-literal fallback, so this deferral can't itself crash
  `_act_gate_node` on an API image that never installed the `temporal`
  extra (`pyproject.toml`'s `temporal` optional-dependencies group).
- **Rejected alternative:** Leave the readiness-gated deferral as-is and
  instead harden `execute_autonomous_live()` itself to refuse code-fix
  action types. Rejected as strictly worse: it duplicates the classification
  logic in two places (drift risk) and still requires exactly this same fix
  at the detection site to avoid the action silently vanishing from the
  ACT report when neither path picks it up.

## E2B sandbox backend removed — K8s Job sandbox is the sole mechanism

- **Decision:** Deleted `sre_agent/code_sandbox.py` and its test file
  entirely (`apply_and_test`, `apply_and_test_e2b`, `run_code_fix`, the
  `SANDBOX_BACKEND`/`E2B_API_KEY` env vars, `pyproject.toml`'s `sandbox`
  optional-dependency group). The only code-fix sandbox mechanism going
  forward is `sandbox_workflow.py`'s Temporal-orchestrated, K8s-Job-based
  `CodeFixVerificationWorkflow`, which dispatches to the `sandbox_real` edge
  MCP server (`edge_mcp_servers/mcp_servers/sandbox_real/`).
- **Reason:** `code_sandbox.py` had zero production callers — only its own
  test file referenced it (confirmed by repo-wide grep). It was a second,
  parallel sandbox mechanism (local subprocess + an E2B microVM backend
  added 2026-09-04) that duplicated what the K8s Job path already does live,
  while adding a paid, metered cloud dependency (E2B has no free tier) and
  cost/timing-noise exposure for `benchmarks/bench_mttr.py`, which drives
  real incidents through the live API. User decision: keep local K8s Jobs,
  drop E2B.
- **Consequences:** `docs/COMPETITIVE_AUDIT.md`'s "Highest-leverage
  upgrades" list drops from 5 items to 4; `.env.example` no longer documents
  `SANDBOX_BACKEND`/`E2B_API_KEY`; `pyproject.toml` no longer has a `sandbox`
  extra. No production code path is affected since nothing called
  `code_sandbox.py`.
- **Rejected alternative:** Keep only the local-subprocess half of
  `code_sandbox.py` (drop just the E2B function) as an unused fallback.
  Rejected — it would still have zero callers and duplicate the K8s Job
  path's purpose; keeping a dead module half-alive serves no one.

## Platform-to-edge MCP traffic crosses a shared Docker network, not published host ports

- **Decision:** `platform/docker-compose.yaml` and `edge_mcp_servers/
  docker-compose.yaml` both join an external Docker network
  (`sre-shared-network`, service alias `shared-edge-network`, created once
  via `docker network create sre-shared-network`). `sre-agent-api` and
  `temporal-worker` reach each MCP server by container name
  (`http://mcp-k8s:3000/sse`, etc. — see `.env.example`). Each MCP server's
  host-published port stays `127.0.0.1:<port>:3000`.
- **Reason:** the two compose files are independent Compose projects/
  networks. On Docker-in-Docker (this project's Codespace), a container in
  one project reaches the other host's published ports via
  `host.docker.internal`, which resolves to the bridge gateway — not the
  true host loopback interface — so a `127.0.0.1`-bound port refuses the
  connection at the kernel level. This was live in production on the
  Codespace: `sre-agent-api` silently ran every graph invocation with 0 MCP
  tools loaded (`Failed to load MCP tools` WARNING, no crash). A same-day
  first-pass fix rebound all 8 MCP ports to `0.0.0.0` to restore
  reachability, but that regressed `tests/test_mcp_auth.py::
  test_compose_ports_are_loopback_only_and_require_token` — a deliberate P0
  hardening control (`git log`: "Harden P0 trust and safety boundaries",
  "feat: multi-tenant secure access (Phase 4)") pairing loopback-only
  binding with `MCP_SERVICE_TOKEN` bearer auth as defense-in-depth. Exposing
  WRITE-capable servers (`mcp-executor`, `mcp-github-exec`, `mcp-sandbox`) on
  all interfaces was an unacceptable trade for reachability.
- **Consequences:** cross-stack traffic never touches a host port; the
  loopback-only host binding (and the test enforcing it) stays intact.
  Anyone standing up a fresh environment must run
  `docker network create sre-shared-network` before `docker compose up` in
  either directory (both compose files declare it `external: true`).
- **Rejected alternative:** bind all 8 MCP ports to `0.0.0.0`. Rejected —
  regresses a deliberate security control for servers that can write to
  GitHub, execute K8s mutations, and run sandboxed code, in exchange for
  fixing a problem a shared network solves without exposure.

## Live dispatch routes on capability, not on the action's name
- **Decision:** `executor.live_tool_for_action(action)` is the single answer
  to "can Sentinel actually execute this?" for infra actions. Membership in
  `EXECUTOR_TOOL_MAP` is necessary but not sufficient: `patch` and
  `config_change` resolve to a tool only when the action carries a cpu or
  memory limit. All three layers consult it — `act_phase` (plan),
  `mutation_gateway` (authorization), `executor` (dispatch).
- **Reason:** `EXECUTOR_TOOL_MAP` mapped the *name* `config_change` to
  `patch_resource_limits`, whose entire surface is container cpu/memory
  limits. "Config change" in SRE vocabulary means env vars, ConfigMaps and
  feature flags far more often, and that is what the planner emits. Live on
  incident `81c3127a`: the planner correctly proposed an `inspect_only`
  config dump and a runtime `SLOW_QUERY_RATE` toggle; a human approved both;
  the MCP server then refused each with "provide at least one of memory/cpu",
  which reads as a planner bug and is really a missing capability. The plan
  was presented as executable when nothing in the stack could execute it.
- **Consequences:** an action with no tool behind it is `blocked` in the plan
  a human reads, with `missing_capability_reason()` as the reason, and is
  counted separately from out-of-namespace blocks in the ACT summary — a
  capability gap is not a policy call. It is never dry-run either, so no
  `kubectl apply -f <rendered-config for X>` placeholder is printed as if it
  were a command. Adding a real config-change tool later is purely additive:
  teach `live_tool_for_action` the new tool and the three layers follow.
- **Rejected alternative:** require the planner to always emit `memory`/`cpu`
  for `config_change` (prompt or schema constraint). Rejected — it forces the
  planner to lie about what it intends, and would have produced a *resource
  limit change* for an incident whose root cause was a runtime feature flag.

## Config changes execute from parameters; inspection is its own action type
- **Decision:** Two additions close the capability gap the decision above only
  reported. (1) `patch_deployment_env` on the executor MCP server is the second
  executable configuration surface: `live_tool_for_action` routes a
  `patch`/`config_change` to `patch_resource_limits` when the action carries a
  cpu/memory limit, to `patch_deployment_env` when it carries `parameters.env`,
  and to nothing otherwise. (2) `inspect` is a first-class read-only action type
  (`RemediationAction`, `EXECUTOR_TOOL_MAP` → `get_deployment_config`,
  `Reversibility.READ_ONLY`), short-circuited to AUTONOMOUS in `policy_gate`
  before the severity, telemetry and calibration gates.
- **Reason:** the planner encoded both intents as prose inside `config_change`
  because it had nowhere else to put them — a runtime flag toggle and a config
  dump. The first had no tool; the second was marked risky, gated, and burned a
  human approval on a step that writes nothing. The planner prompt now documents
  both parameter shapes, so intent is structured data rather than prose a
  downstream layer has to guess at.
- **Consequences:** live mutation surface now includes arbitrary env vars on a
  deployment, so the edge guardrails carry the weight: a credential-name
  denylist that is always on (`PASSWORD|TOKEN|SECRET|API_KEY|…`), an optional
  operator allow-list (`EXECUTOR_ALLOWED_ENV_KEYS`), caps on key count (10) and
  value length (1024), refusal to overwrite a `valueFrom` entry, and exact
  `prior_env` capture for the audit trail. Agent-side, `build_command` redacts
  credential-named values so a *refused* write is not what writes the secret
  into the audit record. Env patching is read-modify-write because a k8s
  strategic merge on `env` replaces the whole list. `NON_MUTATING_ACTIONS`
  (escalate + inspect) now gates verification and skill learning, so a config
  dump can never be graded as a fix.
- **Rejected alternative:** let `inspect` skip the mutation gateway like
  `escalate` does. Rejected — a read still touches a tenant's cluster, and the
  gateway is where namespace scope is enforced; reading another tenant's
  deployment is a data leak, not a harmless no-op.

## External alert recovery closes remediation authority, not investigation

- **Decision:** Human resolution and Alertmanager recovery have separate side
  effects. Human `acknowledge`/`mark resolved` cancels investigation jobs.
  Alertmanager clear marks the incident resolved, lets the current in-process
  investigation finish and post findings, expires every pending graph/Temporal
  approval, and denies waiting Temporal gates. Durable RESOLVED state is checked
  at proposal, approval, ACT, PR, and cluster-mutation boundaries; final graph
  persistence cannot overwrite it.
- **Reason:** Alert silence is not recovery evidence: a restart clears an OOM
  alert and backoff can clear a restart-rate alert. Cancelling on that signal
  discarded paid-for diagnosis, while leaving approvals usable could authorize
  a write for a condition that had already stopped firing.
- **Consequences:** PostgreSQL incident-row locks define the order of a racing
  clear and write and remain held through the external mutation call. Slack
  explicitly says that findings may continue but no approval/write will follow.
  A process crash after the clear remains a separate limitation: the existing
  resolved-incident startup guard refuses automatic retry, so post-clear
  completion is not yet crash-resumable.
- **Rejected alternative:** Keep one shared resolution helper that always
  cancels work. Rejected because human intent and an external telemetry signal
  have different semantics; preserving the shared behavior loses the evidence
  the investigation already gathered.

## Live ACT remediation checkpoints one action per Temporal activity

- **Decision:** `act_phase` serializes the exact policy-approved action batch,
  but a dedicated `LiveRemediationWorkflow` executes it one activity at a time.
  The graph starts or joins a deterministic incident/action-hash workflow ID;
  the existing mutation gateway remains the only write authorization and
  idempotency boundary. The existing `IncidentRemediationWorkflow` remains the
  separate two-gate code-fix/verification/PR state machine.
- **Reason:** LangGraph checkpoints around ACT, not inside its former action
  loop. A process death could therefore restart a whole batch after an earlier
  write succeeded. Temporal activity completions give each successful action a
  durable resume point without duplicating policy, tenant, or target logic.
- **Consequences:** A replacement worker resumes at the first incomplete
  action. It re-reads durable incident state through the mutation gateway; an
  external clear returns `incident_resolved`, stops all later activities, and
  preserves already-completed results for truthful reporting. `EXECUTOR_LIVE`
  now fails closed unless Temporal is enabled and an incident ID is present.
- **Rejected alternative:** Add checkpoints inside the code-fix workflow.
  Rejected because infrastructure actions do not share its patch generation,
  sandbox verification, or two human approval gates; combining the contracts
  would make both workflows harder to reason about.
