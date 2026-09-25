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
  `infra/local/docker-compose.yaml` with no production tenant traffic yet,
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

## Temporal retries stop at the idempotency-claim boundary

- **Decision:** A live-action activity may retry at most three times only when
  the mutation gateway proves failure occurred before the idempotency claim was
  attempted. Claim, dispatch, tool-response, and audit uncertainty is terminal,
  requires manual review, and stops every later action in the batch.
- **Reason:** Retrying a failure after external dispatch can repeat a successful
  mutation; retrying after an uncertain atomic claim can instead misreport an
  unexecuted action as a duplicate. Neither outcome is safe to automate.
- **Consequences:** Setup and pre-claim failures use a typed retryable error.
  Post-claim `ERROR` results remain durable, MCP teardown cannot replace a
  successful activity result, and exhausted retries return an explicit manual
  terminal result rather than failing the whole workflow invisibly.
- **Rejected alternative:** Retrying every transport exception, releasing the
  idempotency claim after an error, or continuing later actions when an earlier
  mutation's outcome is unknown.

## Reflector re-investigation is a bounded graph-owned loop

- **Decision:** When the reflector identifies material unknowns, it may route
  through `investigation_swarm` and back to itself at most
  `MAX_INVESTIGATION_DEPTH` times. Recommendations are normalized onto the
  fixed Kubernetes, metrics, logs, and GitHub agent allowlist; executable agent
  instances remain graph-bound and never enter checkpointed state.
- **Reason:** The state and reflector claimed this loop existed, but the graph
  always routed directly to the planner. Trusting arbitrary model-produced
  names as graph targets would fix the dead branch by introducing a control-flow
  vulnerability.
- **Consequences:** Only recommended evidence agents rerun, the durable counter
  prevents unbounded model/tool cost, invalid names fall through to planning,
  and Langfuse receives stable repeated node names that render as a cycle or an
  expanded per-call DAG.
- **Rejected alternative:** Delete the branch despite its complete state/model
  contract, rerun every specialist, or store agent callables in durable state.

## Per-cluster credentials relay over the MCP transport (Phase 4)

- **Decision:** `services/edge_mcp_servers/*` keep resolving credentials from static
  process environment variables (`GITHUB_TOKEN`/`GITHUB_REPO`, `KUBECONFIG`)
  as their fallback, but a request-scoped credential relay now takes
  priority: `src/sre_agent/multitenant/relay_auth.py::build_relay_headers`
  attaches one cluster's resolved GitHub/K8s credentials as additional
  `X-Sentinel-Relay-*` headers on the MCP connection built fresh per
  investigation (`build_mcp_server_config`); the edge-side ASGI bearer-auth
  middleware (`mcp_auth.py`) captures them into a `contextvars.ContextVar`
  (`services/edge_mcp_servers/relay_credentials.py`), and only two choke points read
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
  (`src/sre_agent/multitenant/github_app.py`) was removed 2026-09-08 since the
  platform authenticates to GitHub with a repo URL + PAT exclusively;
  `Cluster.github_app_installation_id` (DB column) is now unused dead
  storage, kept only to avoid an extra migration. Slack similarly moves from a single
  global `SLACK_BOT_TOKEN` to a per-`Organization` OAuth-installed token
  (`src/sre_agent/multitenant/slack_oauth.py`, `Organization.slack_bot_token`),
  with the env var kept as the self-hosted fallback.
- **Rejected alternative:** A large mechanical rewrite threading a
  `Cluster`/credentials object through every MCP tool handler signature, or
  giving each `Cluster` its own edge deployment (defeats the point of a
  shared control plane and multiplies operational cost per tenant).

## AIOpsLab adapter plays back one investigation, not a live shell loop (Phase 5)

- **Decision:** `evals/benchmarks/aiopslab_adapter.py` does not reuse
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
  `src/sre_agent/executor.py::build_command()`'s output verbatim), then the
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
  (`services/edge_mcp_servers/mcp_servers/runbooks_local/`, `src/sre_agent/runbooks_corpus.py`)
  across all three consumers, not just the human-facing catalog API: (1)
  `src/sre_agent/api/v1/runbooks.py` now reads only via `src/sre_agent/notion_runbooks.py`,
  returning an empty list / 404 for a cluster with no Notion database
  configured, no local fallback; (2) the agent's live-investigation RAG tool
  is now served by `services/edge_mcp_servers/mcp_servers/runbooks_notion/server.py`
  (new, replaces `runbooks_local`), keeping the exact tool names/signatures
  (`search_runbooks` et al.) so `context_builder.py`/`graph_builder.py`'s
  by-name tool lookups needed no changes; (3) `src/sre_agent/runbook_generator.py`'s
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
  source of truth, and — since `services/edge_mcp_servers/mcp_servers/*` images ship to
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

- **Decision:** `src/sre_agent/agent_nodes.py` (specialist message capture) and
  `src/sre_agent/supervisor.py` (final synthesis capture) each independently
  detect `isinstance(content, list)` and join only `type == "text"` blocks,
  rather than adding a single shared normalization utility.
- **Reason:** Both call sites are small, already-distinct extraction points
  (one per streamed chunk in a specialist's message loop, one on a single
  final LLM response), and `src/sre_agent/narrative.py::_invoke_llm()` already
  has its own independent (correct) version of this same normalization —
  three call sites already existed with this pattern in some form before this
  fix; consolidating now would touch more surface than the bug required.
- **Consequences:** Any *new* code path that reads `.content` off an
  Anthropic `AIMessage` and assumes `str` needs the same guard added by hand
  — this is a landmine that can recur. Search for `\.content\b` assignments
  from `AIMessage`/`response` objects before trusting `.strip()`/string ops
  on them.
- **Rejected alternative:** A shared `normalize_ai_content()` helper in
  `src/sre_agent/llm_utils.py` — deferred as unnecessary scope for a bug fix;
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

- **Decision:** `src/sre_agent/executor.py::_live_args()` extracts a k8s resource
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

- **Decision:** `src/sre_agent/incident_correlation.py::correlate`'s optional
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
- **Consequences:** Implemented 2026-09-03 in `src/sre_agent/service_topology.py`
  (`build_adjacency_map` + `get_adjacency_map`), wired into
  `_record_correlation_shadow` in `src/sre_agent/api/v1/alerts.py`. Uses
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
  autonomous actor in `src/sre_agent/actor_runtime.py::HermesRuntime`) remains
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
- **Consequences:** Items 1 and 2 fixed in `src/sre_agent/actor_runtime.py` /
  `src/sre_agent/incident_remediation_workflow.py` (tests added in
  `tests/test_actor_runtime.py`), independent of whether `hermes-agent` is
  ever installed — both are correct regardless of the package's actual
  internals. Item 3 remains open and is the actual blocker on enabling
  `AGENT_RUNTIME=hermes`: before flipping that env var anywhere real,
  either (a) install `hermes-agent` in a disposable environment and
  introspect its real toolset names to build an explicit `enabled_toolsets`
  allowlist, or (b) run `HermesRuntime` inside the project's existing
  ephemeral-K8s-Job sandbox infra (`services/edge_mcp_servers/mcp_servers/sandbox_real/`)
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
  `src/sre_agent/actor_runtime.py`. `get_agent_runtime()` now unconditionally
  returns `LocalTerminalRuntime` — the actor is no longer pluggable, since
  there is only one implementation. `hermes-agent` is dropped from
  `pyproject.toml`'s optional extras and `uv.lock`; the Hermes-specific
  tests in `tests/test_actor_runtime.py` are deleted; the `task_id=` kwarg
  (Hermes's memory-isolation key) is removed from
  `incident_remediation_workflow.py`'s `generate_patch_activity` call site.
  `src/sre_agent/skill_store.py` (the self-improving skill-memory module,
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
  exists in the codebase. The contemporaneous design notes were later removed
  during documentation cleanup; the retained competitive audit lives at
  `docs/archive/audits/COMPETITIVE_AUDIT.md` and is historical rather than a
  statement of current behavior.
- **Rejected alternative:** Keep `HermesRuntime` in place, unused but
  available behind the env var, in case a future need for an autonomous
  third-party actor arises. Rejected because unused, unreviewed-to-safety
  optional code paths are exactly the kind of latent risk this session's
  own process already flagged (proceeding on a stale "blocker" framing
  instead of first checking whether the code path was needed at all) —
  dead optionality that nobody will re-review before flipping on is worse
  than no optionality.

## Phase E cutover: code-fix actions always defer to the deterministic pipeline

- **Decision:** In `_act_gate_node` (`src/sre_agent/graph_builder.py`), detecting
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

- **Decision:** Deleted `src/sre_agent/code_sandbox.py` and its test file
  entirely (`apply_and_test`, `apply_and_test_e2b`, `run_code_fix`, the
  `SANDBOX_BACKEND`/`E2B_API_KEY` env vars, `pyproject.toml`'s `sandbox`
  optional-dependency group). The only code-fix sandbox mechanism going
  forward is `sandbox_workflow.py`'s Temporal-orchestrated, K8s-Job-based
  `CodeFixVerificationWorkflow`, which dispatches to the `sandbox_real` edge
  MCP server (`services/edge_mcp_servers/mcp_servers/sandbox_real/`).
- **Reason:** `code_sandbox.py` had zero production callers — only its own
  test file referenced it (confirmed by repo-wide grep). It was a second,
  parallel sandbox mechanism (local subprocess + an E2B microVM backend
  added 2026-09-04) that duplicated what the K8s Job path already does live,
  while adding a paid, metered cloud dependency (E2B has no free tier) and
  cost/timing-noise exposure for `evals/benchmarks/bench_mttr.py`, which drives
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

- **Decision:** `infra/local/docker-compose.yaml` and `services/edge_mcp_servers/
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
  persistence cannot overwrite it. If downtime loses the resolved webhook, a
  periodic recovery may synthesize that lifecycle clear only after the original
  durable Alertmanager job identity maps to an existing, healthy Prometheus rule
  and two rule snapshots separated by a persisted grace interval show no matching
  active series.
- **Reason:** Alert silence is not recovery evidence: a restart clears an OOM
  alert and backoff can clear a restart-rate alert. Cancelling on that signal
  discarded paid-for diagnosis, while leaving approvals usable could authorize
  a write for a condition that had already stopped firing.
- **Consequences:** PostgreSQL incident-row locks define the order of a racing
  clear and write and remain held through the external mutation call. Slack
  explicitly says that findings may continue but no approval/write will follow.
  Missing rules, unhealthy evaluation, malformed responses, and query failures
  leave the incident open. A compare-and-set permits only one late webhook or
  recovery replica to publish closure effects. The durable absence observation
  survives a control-plane restart, but is explicitly not remediation
  verification.
- **Rejected alternative:** Keep one shared resolution helper that always
  cancels work. Rejected because human intent and an external telemetry signal
  have different semantics; preserving the shared behavior loses the evidence
  the investigation already gathered. A single empty `ALERTS` query or an
  expired `endsAt` was also rejected because either can reflect monitoring
  failure or stale delivery rather than a cleared condition.

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

## API and Temporal worker share one fingerprinted runtime image

- **Decision:** Build the platform Python source once as `sentinel/api:local`
  and recreate both API and Temporal-worker entrypoints from that image. The
  image contains a build-time SHA-256 manifest of all `src/backend/` and
  `src/sre_agent/` Python files. Startup verifies the manifest; the worker also
  imports every workflow/activity dependency; deployment compares both running
  code revisions, fingerprints, and file counts.
- **Reason:** The live crash-resume probe failed before its first mutation
  because the worker image lacked `executor.NON_MUTATING_ACTIONS` while the API
  and repository had it. Individually healthy processes do not prove their
  workflow/activity contracts are compatible.
- **Consequences:** A partial/manual source copy fails on restart or health
  check instead of accepting Temporal work. `deploy_agent_runtimes.sh` refuses
  a dirty tracked tree, builds one committed revision once, force-recreates both
  entrypoints, and does not succeed until parity is proven.
- **Rejected alternative:** Compare a few hand-picked module hashes during an
  incident. Rejected because it detects drift only after deployment, misses new
  dependencies, and repeats the exact manual procedure that allowed the worker
  to diverge.

## Raw specialist evidence is artifact-backed, not checkpoint context

- **Decision:** Persist each specialist's lossless tool transcript and final
  response as a gzip-compressed, content-addressed PostgreSQL artifact. Graph
  state retains its digest/reference, compact measured evidence used by policy,
  and at most a bounded head-and-tail view of the final response. Duplicate
  `investigation_findings` copies are no longer produced.
- **Reason:** Tool transcripts are the largest checkpoint values and grow with
  every MCP result. Only measured values and the specialist conclusion are
  needed for active reasoning; retaining raw payloads in every LangGraph
  checkpoint increases serialization, storage, and restart cost without adding
  decision authority.
- **Consequences:** Artifact reads verify the SHA-256 digest and require the
  owning incident ID. References remain visible in checkpoint and timeline
  metadata for Langfuse/audit correlation. If PostgreSQL is unavailable or an
  ad-hoc run has no durable incident ID, the old in-state trace is retained so
  evidence is never silently lost.
- **Rejected alternative:** Treat Langfuse or local JSONL trace files as the
  artifact store. Rejected because Langfuse is optional and the JSONL file is
  process-local; neither is the durable incident-owned recovery boundary.

## Local node metrics and Langfuse have separate truthful contracts

- **Decision:** The in-process recorder measures every top-level LangGraph node
  invocation, including failures; failed duration contributes to total/average
  latency and each failure counts once in the run denominator. Langfuse owns
  model/tool spans, tokens, cost, and routing provenance. Neither API nor UI
  claims a provider switch that no production emitter can observe.
- **Reason:** The partial wrapper omitted prepare, supervisor, specialists, and
  aggregation. Worse, failed invocations increased errors but not runs, allowing
  rates above 100%. A dormant `provider_switch` event and dashboard row implied
  cross-provider failover even though tenant configuration authorizes one
  provider and the constructor tries only that provider.
- **Consequences:** `/agent/metrics` is explicitly process-local graph execution
  telemetry, while Langfuse remains the distributed AI trace. Model accounting
  records `fallback_allowed=false`; a failure preserves its configured provider
  identity instead of silently crossing a credential/authority boundary.
- **Rejected alternative:** Populate the provider-switch UI from requested and
  actual model-name differences. Rejected because a model alias/version change
  within one provider is not cross-provider fallback, and relabeling it would
  manufacture an event rather than observe one.

## Runtime owns successful durable-job completion

- **Decision:** `run_graph_background_saas()` is the sole successful terminal
  writer for an investigation job. The queue worker owns the lease while the
  run executes and records only exceptions that escape the canonical runner.
- **Reason:** The runtime has the incident status, verification, audit result,
  trace completeness, and dashboard payload needed for one atomic terminal
  write. The worker's second `complete_job()` call discarded that context and
  raced or rejected the already-completed row.
- **Consequences:** A successful run has one authoritative `COMPLETED` or
  `DEGRADED` transition. Runtime failure handling remains retry-aware; worker
  finalization is reserved for uncaught/pre-runtime failures.
- **Rejected alternative:** Make the worker complete with `{"ok": true}` and
  leave the runtime's rich update in place. Rejected because it creates two
  owners and can turn a retryable runtime failure into a completion attempt.

## Aggregate narration gets deterministic status grounding

- **Decision:** `build_supervisor_summary_content()` accepts the durable
  incident status and appends `grounding_footnote()` when model-authored
  wrap-up text claims a contradictory phase or omits the exact next command.
- **Reason:** Follow-up answers already had this correction, but the initial
  aggregate summary was another Slack-visible model surface with no equivalent
  status check.
- **Consequences:** The model's wording remains intact for auditability; the
  timeline content and payload carry the correction and status together. Calls
  without a status retain their backward-compatible behavior.
- **Rejected alternative:** Rewrite or prompt the model to obey the status.
  Rejected because the existing follow-up incident showed that prompting alone
  still produced contradictory phase claims.

## Sentinel is Anthropic-only; multi-provider is not being pursued

- **Decision:** `provider_config.SUPPORTED_PROVIDERS` stays `("anthropic",)`.
  Every other value — including one supplied per tier via
  `MODEL_ROUTER_<TIER>_PROVIDER`, which previously bypassed the check — is
  rejected at the point of configuration with a migration message. The model
  router varies the Claude model per task type on a fixed ladder
  (haiku-4-5 → sonnet-4-5 → sonnet-5 → opus-5); it does not vary vendors.
- **Reason:** One provider is the only configuration that is exercised end to
  end. The specialist and planner nodes depend on reliable tool/function-call
  structured output, and the dead Gemini/Groq/NVIDIA config surface advertised
  a portability the runtime refused at startup — every doc, chart value, and
  example secret naming those providers pointed an operator at a guaranteed
  CrashLoopBackOff.
- **Consequences:** The honest claim is "static task tiers on the Anthropic
  ladder", not "adaptive multi-provider routing". `ANTHROPIC_MODEL` must stay
  on the ladder or per-tier escalation silently falls back to the router's
  fixed tier defaults. `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` survive as
  single-tenant fallbacks for an Anthropic-compatible gateway, read only when
  no cluster is bound (`cluster_context.resolve_llm`). `RequestContext` and
  the `complexity` axis in `model_router` stay implemented and tested but are
  documented as having no production caller.
- **Rejected alternative:** Keeping the multi-provider config surface as a
  "future option". Rejected because unused config that fails closed at startup
  is not an option, it is a false claim with a deployment cost.

## Context is budgeted against a declared window with the reply reserved

- **Decision:** `context_compaction` counts tokens with tiktoken (plus a
  safety ratio, because Anthropic ships no local tokenizer) instead of
  `len(text)//4`, and derives a hard input ceiling of
  `CONTEXT_WINDOW_TOKENS − CONTEXT_RESERVED_OUTPUT_TOKENS − margin`. That
  ceiling is enforced in two places: a deterministic `pre_model_hook` on every
  specialist's ReAct loop (`agent_nodes.py`), and a no-LLM backstop in
  `agent_runtime._maybe_compact` that runs even when the summarizer call
  fails. The window is **operator-declared, not discovered** — there is no
  hardcoded per-model table.
- **Reason:** Compaction ran pre-run, over `state["messages"]` — the one list
  a specialist never touches. Specialists run on an isolated
  `[system, user]` pair and do not return `messages`, so the loop that
  actually accumulates tool output (a single `kubectl get -o json` is tens of
  thousands of tokens, re-sent every iteration) had no budget at all. The
  character heuristic also scored tool-call arguments as free, since the
  requesting message's `content` is empty. And a budget equal to the full
  window is a guaranteed 400: the model needs room to answer.
- **Consequences:** The hook returns `llm_input_messages`, so it rewrites only
  the model's view — graph state keeps the full transcript, which is what
  `tool_failures` detection and `evidence_artifacts` read, and is why
  in-prompt truncation loses no evidence. Trimming sacrifices in order: shrink
  oldest tool results (head+tail kept, middle elided), drop whole oldest turn
  groups with a counted note, then hard-truncate. Cuts are always on turn-group
  boundaries because an orphaned `tool_result` is a hard provider rejection —
  the old `messages[-keep_recent:]` slice could produce exactly that.
  `CONTEXT_ITERATION_MAX_TOKENS` originally defaulted to the full ceiling —
  **reversed on 2026-09-19, see the entry below.** `tiktoken` is now a declared
  dependency; its
  first-use BPE fetch is attempted exactly once per process and never retried,
  so an air-gapped cluster degrades to the character heuristic instead of
  hanging a hot-path budget check (`CONTEXT_TOKENIZER=heuristic` skips it).
- **Rejected alternative:** A per-model context-window table keyed on model id.
  Rejected because the numbers are unverifiable from inside the process, model
  ids turn over faster than the table would be maintained, and a stale entry
  fails in the same silent, expensive way the character estimate did.

## Retrieval is measured in two separate halves, and every offline label is derived

- **Decision:** Retrieval quality is split into label-free runtime
  instrumentation (`src/sre_agent/retrieval_metrics.RetrievalRecorder`, surfaced at
  `/agent/metrics → retrieval`) and labeled offline ranking metrics
  (`evals/benchmarks/retrieval_eval.py`). The two never merge. Offline labels are
  **derived** from contracts the code already commits to — self-retrieval,
  taxonomy-generated paraphrase, tenant/cluster isolation, distractor
  rejection, invalidation — and no relevance judgment is hand-assigned. The
  gate fires on `mrr`, `hit_rate` and `false_positive_rate`; `precision_at_k`
  is reported but never gated.
- **Reason:** Memory, verified skills, and runbooks all degrade to "returns
  zero results" when they break — dead store, tenant filter matching nothing,
  a re-embedded collection, a changed embedding dimension — and zero results
  is indistinguishable from "nothing relevant exists" at every call site, so
  the learning claim was unfalsifiable. Call-shape metrics catch most of that
  and need no labels. Relevance does need labels, and
  `evals/benchmarks/datasets/README.md` already forbids invented ones; a derived
  label is wrong only if the contract behind it is wrong, which is a bug worth
  failing on. Precision is excluded from the gate because `match_score` gives
  a same-class skill on a different service 0.5 by design, so that candidate
  is intended behavior, not a false positive.
- **Consequences:** `RetrievalEvent` carries no query text, no document text
  and no ids — shapes and numbers only, consistent with the audit-redaction
  work. The instrumented wrapper lives on `InMemorySkillStore.find_matching`
  with implementations moved to `_find_matching`, so `SemanticSkillStore`'s
  `super()` call cannot double-count. Because all three stores swallow their
  own exceptions, `track_retrieval` reads `observed["error"]` rather than
  relying on propagation; a recorder that throws is swallowed, since a metrics
  ring buffer must never fail an investigation. The no-relevant-documents case
  is routed to `false_positive_rate` instead of being scored 0.0 or skipped:
  for an SRE agent a confident irrelevant incident is worse than nothing,
  because the model reasons from it. The Qdrant-backed memory half runs only
  with `--qdrant-url` and reports `{"status": "skipped"}` otherwise — never a
  pass it did not earn.
- **Rejected alternative:** One combined retrieval score mixing production
  call shape with offline relevance. Rejected because it would let a healthy
  empty-rate mask a recall regression, and because publishing a relevance
  number computed over unlabeled production traffic is the specific dishonesty
  this work exists to remove.

## Benchmark scenarios are bounded by a digest-pinned fixture capability manifest

- **Decision:** Dataset v2 (`evals/benchmarks/datasets/v2/`) adds `fixtures.json`, a
  manifest of the fault surface the Meridian reference workload actually
  exposes: every adapter-drivable target with its `/admin/config` path and each
  knob's type, bounds and healthy baseline; every Prometheus alert rule a
  scenario may claim to have fired, with the severity and service that rule
  itself emits; and every metric series a recovery probe may query. Its SHA-256
  is pinned in `dataset.json` alongside the three split digests, and
  `load_dataset` enforces the manifest for every scenario. Schema 2 also
  replaces the single `fault.target`/`inject`/`cleanup` triple with an ordered
  `fault.contracts[]`, and `scenario_dataset.py --repin` becomes the only
  sanctioned way to re-pin digests.
- **Reason:** "No invented fixtures" was README prose, and v1 had already
  violated it in a way nobody could see: `bad_deploy_checkout`'s probe demanded
  a checkout error ratio below 0.05, but `reserve_inventory_hold` fails a fixed
  7-in-20 hash bucket, so the service sits near 35% at rest and that scenario
  could only ever have reported `INVALID_SCENARIO` on a live cluster. A
  scenario that cites a knob, alert, or metric the workload does not have is
  not a failing test — it is a benchmark measuring nothing, and it fails far
  from the edit that caused it. The manifest turns that class of error into a
  load-time `DatasetError` that CI catches with no cluster. Multi-contract
  faults exist because the interesting scenarios — noisy neighbour, dependency
  cascade, compound failure — are precisely the ones a single-target schema
  cannot express, and those are what separate a diagnosis from a guess.
- **Consequences:** A scenario is rejected at load time when it names an
  undeclared target, knob, alert or metric; serves a config path the target
  does not; injects a wrong-typed or out-of-range value, or the declared
  healthy baseline; cleans up to something that is not the real baseline; or
  claims a severity or service its alert rule does not emit. Every manifest
  bound carries a `reference` into the workload source, so changing the
  workload without changing the manifest is now a visible inconsistency rather
  than silent drift. The adapter applies contracts in order and unwinds every
  contract already applied if a later one fails, so a partial injection cannot
  leak into the next scenario; cleanup restores in reverse and attempts every
  lease before raising. Contracts are normalized at load time, so
  `fault_adapter.py` and `sre_bench.py` only ever see `contracts` and schema 1
  keeps working unchanged. `--repin` re-loads all three splits before writing,
  so it can restore content addressing but can never bless a dataset that does
  not validate. Two declared alert rules are deliberately unreachable
  (`InventoryMemoryApproachingLimit`, `PaymentServiceUnhandledErrors`); they
  stay in the manifest so it describes the real rule set, not a convenient
  subset.
- **Rejected alternative:** Keeping one fault target per scenario and trusting
  the README. Rejected because it caps the corpus at single-service faults —
  no noise, no cascade — which is the half of the space where an SRE agent
  actually fails, and because the v1 defect proves prose does not hold a
  contract that nothing checks.

## The autonomy threshold is chosen by declared cost, and only live evidence can grant one

- **Decision:** Confidence calibration artifacts (schema 2) record a full
  `threshold_curve` over every candidate operating point plus an explicit
  `cost_model` (`false_autonomy_cost` vs `abstention_cost`), and the threshold
  is the cheapest eligible point under a recorded `selection_rule` rather than
  the first point clearing a hardcoded Wilson bound. Every record declares an
  `evidence_source`; only an all-`live_benchmark` corpus can produce an
  artifact carrying a threshold.
- **Reason:** The old 0.90 Wilson floor was decorative — it was asserted, not
  derived, carried no rationale, and could not be argued with because nothing
  recorded what it was trading off. Worse, the rule that synthetic evidence
  must never enable autonomy lived only in `evals/benchmarks/confidence/README.md`
  prose, so a fabricated JSONL carrying the right `config_fingerprint` would
  have produced a fully valid autonomy-granting artifact. That is the same
  defect class as the v1 fixture prose fixed in the dataset manifest decision:
  a safety contract nothing checks.
- **Consequences:** The floors survive as an eligibility constraint, not as the
  selector — an operating point that cannot be shown to work is never eligible
  however cheap it looks. Raising `false_autonomy_cost` demonstrably buys a
  stricter threshold and less coverage; the tests pin that relationship, so the
  threshold is now falsifiable. `load_calibration_artifact` recomputes the
  curve, the three policy costs, and the selected point from the bins and the
  cost model, so a hand-edited and re-digested artifact fails to load — as does
  one whose threshold did not come from live evidence. Artifacts that grant no
  autonomy must say why, and that reason reaches the operator through
  `ActReport.autonomy_blocked_reason` instead of an undifferentiated
  "uncalibrated". `evals/benchmarks/sre_bench.py` is the only sanctioned producer of
  `live_benchmark` records, which means no real artifact can be built without a
  paired A05 run against a live cluster.
- **Rejected alternative:** Keeping the Wilson floor as the selector and adding
  the cost model as reporting only. Rejected because it leaves the number that
  actually gates production unexplained, and because a curve nobody selects
  from is decoration of a different kind.

## An ablation arm is a configuration fingerprint, and the manifest — not the operator — attests it

- **Decision:** `SENTINEL_ABLATION_ARM` selects one of four measurement
  configurations (`full`, `single_agent`, `no_reflector`, `no_memory`), each
  removing at most one component. Unset is production, not an arm. The arm is
  written into the run manifest's `runtime` section, which is one of the four
  sections the A01 configuration fingerprint hashes, so two arms are
  structurally incomparable by construction. `evals/benchmarks/ablation_eval.py`
  requires every arm — control included — to present the manifest it actually
  ran under, recomputes `configuration_fingerprint()` from it, and refuses to
  proceed unless that hash equals the fingerprint recorded on the arm's
  trials, the manifest names the claimed arm, `ablation_experiment` is true,
  `learned_memory_writes` is false, and the `code_sha` matches the control's.
- **Reason:** Three of the four architectural claims in the HolmesGPT
  comparison — multi-agent, reflection, memory — were asserted from the
  diagram. Measuring them needs arms, and arms need provenance:
  `BENCH_CONFIG_FINGERPRINT` is operator-declared, so without manifest
  attestation an operator could hand in two runs of the *same* arm under
  different fingerprints and `compare_candidates` would compare the full stack
  to itself and report a confident null. That is the worst possible output —
  a number that looks like evidence of no effect.
- **Consequences:** Learned-memory writes are frozen in every arm including
  the control, because arms run sequentially against one cluster and a control
  that wrote what it learned would hand the next arm a corpus it never had;
  the comparison would measure run order. An unknown arm name raises
  `AblationError` at startup rather than degrading to the control. Two naive
  arms would have been strawmen and are not: `no_reflector` passes the
  specialists' findings to the planner directly (the reflector was their only
  channel), still wrapped in the untrusted-content boundary, and
  `single_agent` writes its findings where the reflector can read them.
  `single_agent` keeps `aggregate`, so report quality stays comparable, and
  holds the explicit union of every specialist's read-only tools so the
  baseline measures architecture rather than tool access. `no_memory` keeps
  static runbooks, which are authored rather than learned.
- **Rejected alternative:** Reusing `compare_candidates`' PROMOTE/BLOCK as the
  ablation verdict. Rejected because it is a non-inferiority release gate — a
  bar a component that does nothing clears easily. The harness applies its own
  superiority rule (lower bound of the paired full-minus-arm diagnosis delta
  strictly above zero) and reports an interval containing zero as
  `NOT_DEMONSTRATED`, annotated with why the evidence was too thin to call it
  a null when it was. Diagnosis is the versioned exact service/fault-mode
  criterion and is deliberately independent of recovery: human approval gates
  leave recovery and end-to-end quality at zero. Those outcomes remain in the
  report and retain production release authority; diagnosis evidence alone
  never authorizes rollout or remediation.

## The specialist split is not a swarm, but the node id stays `investigation_swarm`

**Decision.** Every string an operator reads calls this what it is — a
supervisor-routed split of specialists with a bounded re-investigation loop.
The LangGraph node id `investigation_swarm` is frozen and not renamed.

**Reason.** A swarm is peer-to-peer handoff with no central router. This graph
routes every transition: the supervisor picks which specialists run, the
reflector picks which run again. Calling it a swarm claimed an architecture
the code does not implement, and it claimed it in log lines, in the
reflector's thought trace, and in the design docs — the three places a
reviewer looks. The node id is different in kind: it is written into LangGraph
checkpoints and into Langfuse span names. Renaming it would fail resume for
every in-flight incident and split the trace history at the rename, which is a
real operational cost paid for a cosmetic gain.

**Consequences.** `tests/test_deeper_investigation_loop.py` fails if any line
of `graph_builder.py` mentions a swarm outside the node id itself, so the
language cannot drift back. Anyone reading the node id in a trace will find
the explanation at the node definition and here.

**Rejected alternative.** Renaming the node and writing a checkpoint
migration. The migration is possible but buys nothing an operator can see, and
it would have to be correct on the crash-recovery path — the one path where a
bug is least recoverable.

## "Model routing saves cost" is struck, not deferred

**Decision.** Sentinel does not claim that model routing saves money, and the
claim is removed from the standard it was being held to rather than left open
as future work. What the repository claims is exactly this: task-aware static
tiering on a fixed Anthropic ladder, whose cost effect is unmeasured.

**Reason.** The claim was going to be proved with a per-task cost/quality
Pareto frontier and a router-vs-fixed-model experiment. Two things in the
current design make that experiment meaningless. First, the provider contract
is Anthropic-only, so the interesting half of the routing question — cheap
vendor versus expensive vendor — cannot be asked. Second, `complexity` and
`RequestContext` (budget, off-policy) are implemented and tested but no
production call site passes either, so a routing experiment would compare a
static ladder against a static model and measure the ladder, not routing. An
unprovable claim left on a roadmap reads as work in progress; it is really a
claim that will never be settled, and saying so is the honest version.

**Consequences.** `model_router.py` states the cost effect as a hypothesis and
says plainly that no document should assert the saving. `README.md` says the
same and points here. If a future version routes on measured complexity or
across vendors, the claim becomes askable again and this entry should be
revisited — that is a new decision, not a resumption of this one.

**Rejected alternative.** Running the router-vs-fixed-model experiment anyway.
It would produce a real number attached to a question nobody asked, and the
number would be quoted as if it validated adaptive routing, which is exactly
the overstatement the audit was closing.

## Model cost is derived from reported tokens when the client hides `response_cost`

**Decision.** `model_accounting.py` prefers a provider-reported cost and, when
none is surfaced, derives one from provider-reported token counts against
LiteLLM's price table. Every record carries `cost_source` (`provider` or
`derived`); a derived record also carries the exact per-token rates used, and
the rollup exposes `cost_sources` so a total summed from derived parts is never
mistaken for a reported one. Token counts are still never estimated — no usage
means no cost and an incomplete call. Cache-read and cache-creation tokens are
priced at their own rates, since LiteLLM folds both into `prompt_tokens`.

**Reason.** The module previously held that missing values stay missing rather
than being estimated from mutable price tables. Live fire showed that stance
had no reachable success case: `langchain_litellm.ChatLiteLLM` builds its
`llm_output` from `token_usage` and `model` alone and never propagates the
`_hidden_params` where LiteLLM puts `response_cost`, so the reported cost is
structurally unavailable through the only client this system uses. Every one of
the 123 calls in the first live incident recorded `cost_unavailable`, which made
the whole rollup incomplete and nulled `cost_usd` and `tokens` on every trial
record. The real choice was therefore derived-and-labelled versus permanently
null, and permanently null silently disables the cost half of A10 — the
question of whether the architecture earns its cost is the one that ablation
exists to answer.

**Consequences.** Artifact `SCHEMA_VERSION` is 2. `cost_usd` is populated for
any model LiteLLM prices; an unpriceable model still fails closed. The price
table is a mutable dependency, which is why the rates travel with the number
instead of being trusted implicitly. Re-pricing the first live incident from its
recorded tokens yields $8.32, which is the basis of the ~$50/arm and ~$140/four-arm
budget.

**Rejected alternative.** Fixing it at the client — patching or replacing
`ChatLiteLLM` so `_hidden_params` reaches the callback. Rejected as a much
larger blast radius (the wrapper is what gives every agent `with_structured_output`
and tool binding) for a number this system can compute exactly from data it
already records.

## The ReAct conversation prefix is cached, at a 5m TTL

**Decision.** `cache_conversation_prefix` moves an Anthropic `cache_control`
breakpoint to the tail of the transcript before every model call, via a new
`prepare_for_model` hook on the compaction pre-model hook that runs *after*
fitting. The requested TTL default is **5m, not 1h**, and `model_accounting`
prices a cache write against whichever TTL was asked for.

**Reason.** Caching is a billing mechanism, not a context mechanism: the cache
is exact-prefix-match and server-side, so the model receives byte-identical
tokens either way and output quality cannot move. Only the static system prompt
and tool catalog were tagged, and those are not what a ReAct loop spends money
on — the loop re-sends the whole transcript every iteration, so input cost is
quadratic in turn count while the static part is flat. A turn only ever appends
(ToolNode adds an AIMessage and a ToolMessage), so each turn's cached prefix is a
strict prefix of the next turn's request and the read hits.

The 5m default is load-bearing and was not the original choice. A write costs
2x the base input rate at 1h against 1.25x at 5m, while a read costs 0.2x
either way, so a longer TTL buys nothing unless entries survive to be read an
hour later. Nothing here does: a loop rewrites its prefix every few seconds,
and an arm's scenarios are minutes apart. Measured over three turns of a
specialist loop, 1h cost **$0.167** against **$0.134** for the same calls with
no caching at all — the write premium made caching a net loss — while 5m cost
**$0.115**. The gap widens with turn count, since each written increment is
then read by more subsequent turns.

**Consequences.** Artifact `SCHEMA_VERSION` is 3. Records carry
`tokens.cache_read` and `tokens.cache_creation` (a breakdown of `input`, never
an addition; `None`, not `0`, when the provider does not report them), so a
recorded cost can be recomputed from the record alone — which is how the
mispricing below was caught. `_derived_cost` reads LiteLLM's
`cache_creation_input_token_cost_above_1hr` key when the TTL is 1h; pricing
every write at the 5m key understated writes by 60%, which was live for the
whole of the previous window. The tagging is provider-gated to Anthropic in
`agent_nodes.py`, and `prepare_for_model` itself is provider-agnostic.

The `$8.32` re-pricing of the first live incident recorded in the decision
above is an **upper bound, not a measurement**: it prices all 3.37M input
tokens at the uncached rate although the static prefix was already cached. The
~$50/arm and ~$140/four-arm figures derived from it inherit that bias and are
now additionally stale, since the conversation body is cached too.

**Rejected alternative.** Mixing TTLs — 1h on the static system prompt and tool
catalog, 5m on the conversation. Anthropic allows it and the ordering rules
happen to suit our layout, but the static block is a few thousand tokens and
the difference across a six-scenario arm is a few cents, which does not pay for
a second TTL to reason about. Also rejected: tagging before context fitting,
which risks trimming away the breakpoint itself.

## The runbook reaches the agent as a rendered procedure, not as a search hit

- **Decision:** `src/sre_agent/runbook_brief.py` parses the `search_runbooks`
  envelope, picks the best hit, fetches that page's body with a **second**
  `get_runbook_content` call, and renders a budgeted brief that is ordered by
  section priority — remediation first, background last. `ContextBuilder`
  attaches it as `annotations["runbook_context"]`, and
  `resolve_runbook_context()` does the same for the SaaS path in
  `agent_runtime`, which builds `AlertContext` itself. `narrative
  .build_specialist_task_brief` renders it above the alert payload, inside
  `wrap_untrusted`, with an instruction to work its steps in order.
- **Reason:** Three independent defects made the operator's runbook the least
  influential document in an investigation. `context_builder` sliced the search
  result to `str(result)[:500]` — 500 bytes of minified JSON that stops
  mid-key. `search_runbooks` returns only properties plus a 320-character
  keyword excerpt, so even untruncated it never contained a procedure. And
  `ContextBuilder` runs only in local fallback mode, so in production the
  annotation was never set at all. The specialist prompt then never rendered
  `runbook_context` even when present. Measured on the 2026-09-19 validation
  run: the curated runbook reached the agent as 500 bytes while a single Loki
  result reached it as 1,782,133 bytes — 1:3,564 in favour of noise.
- **Consequences:** Two MCP calls per investigation instead of one, against a
  corpus that is small and cached. Sections are dropped **by priority, never by
  position** — a head-first byte budget spends its whole allowance on "Summary"
  and "Background" and runs out exactly where the steps begin. Every omission
  names what was dropped and the `get_runbook_content("<id>")` call that
  retrieves it, so the brief is never silently lossy. An excerpt-only brief
  says so explicitly, because an agent that mistakes a 320-character excerpt
  for the whole procedure reports a partial fix as a complete one. The brief is
  untrusted data: a Notion page is editable by anyone with write access, so it
  goes through `wrap_untrusted` like any other external content, and the
  specialist brief passes an explicit `max_len` because `prompt_guard`'s own
  6000-char default would otherwise cut the verification section off a
  full-size runbook. Every failure degrades — no runbook is a worse
  investigation, an exception here would be no investigation.
- **Rejected alternative:** Raising the 500-char slice to a larger slice.
  Rejected because the search response contains no procedure at any length;
  the missing call, not the missing bytes, was the defect.

## Specialists are told what their peers already found

- **Decision:** `agent_nodes` passes every other specialist's completed result
  into `build_specialist_task_brief` as `prior_findings`, rendered under "do not
  re-derive any of it", each finding bounded by `PRIOR_FINDING_MAX_CHARS`
  (1500) and the set by `PRIOR_FINDINGS_MAX_CHARS` (4000).
- **Reason:** Specialists ran as isolated `[system, user]` pairs with no view of
  the shared transcript, so each one began from the raw alert. The logs
  specialist re-derived, from open-ended discovery, what the runbooks and
  metrics specialists had already established — and open-ended discovery is
  what produces megabyte-scale tool results.
- **Consequences:** Findings are re-bounded on the way in rather than forwarded
  whole: a specialist report is capped at 12k chars for synthesis, and
  forwarding four of those unbounded would add ~12k tokens to a prompt the ReAct
  loop re-sends every iteration — paying for context engineering with the
  blowup it exists to prevent. Ordering is the planner's; a specialist that runs
  first simply receives nothing.
- **Rejected alternative:** Sharing the full message list between specialists.
  Rejected because it reintroduces exactly the quadratic transcript growth the
  isolated-pair design exists to avoid, and most of a peer's transcript is raw
  tool output the peer already summarized.

## Tool results are capped on arrival, not once the transcript is already large

- **Decision:** `fit_to_budget` gained a step 0 that caps any single tool
  result at `CONTEXT_TOOL_RESULT_MAX_CHARS` (20,000, floored at the shrink
  floor), applied to the newest turn group as well and run whether or not the
  transcript is over budget. `CONTEXT_ITERATION_MAX_TOKENS` now defaults to
  `min(60,000, hard ceiling)` instead of the hard ceiling itself, **reversing**
  the decision recorded above.
- **Reason:** Steps 1-3 are reactive — they shrink a transcript that has already
  grown too large, which is one iteration too late. The ReAct loop re-sends the
  whole transcript every iteration, so an unbounded result is paid for once per
  remaining step. On the 2026-09-19 validation run 8 of 114 model calls (7%)
  carried 1,295,598 input tokens — 37% of the run's input and **$3.68 of its
  $7.90 (47%)**. The largest single tool result was 1,782,133 bytes; on the turn
  it arrived the transcript was still under the 185,904-token ceiling, so
  nothing trimmed it. The original reasoning — "trimming exists to prevent a
  failed request, not to save money" — treated the ceiling as the only thing
  worth defending, and missed that the cost of a payload is multiplied by the
  loop length, not paid once.
- **Consequences:** Nothing is lost. `fit_to_budget` rewrites only
  `llm_input_messages`; graph state keeps the full transcript and the evidence
  artifact is built from that, so the audit record is unaffected — which is why
  the cap lives here and **not** in the MCP tool wrapper, where it would have
  corrupted `_artifact_backed_trace_metadata`. The cap keeps head and tail and
  elides the middle, because a log page's tail holds the most recent lines. It
  is reported as `FitReport.capped_results` and logged per specialist. A
  60,000-token working budget means trimming now engages routinely rather than
  never; the specialist brief carries a matching instruction to issue narrow,
  label-filtered, window-bounded queries, so the cap is a backstop and not the
  primary control.
- **Rejected alternative:** Capping at the tool wrapper boundary, which is
  earlier and simpler. Rejected because the evidence artifact is built from the
  same `ToolMessage` objects, so capping there would silently truncate the
  audit record to save prompt tokens.

## Meridian runbooks prescribe one branch per fault, and an audit script proves it

**Decision.** The four curated Meridian runbooks are rewritten as branching
decision procedures: a numbered set of measurements that selects exactly one
`## Branch X — Action: ...` section, each branch naming the MCP tool to call
(with `namespace="meridian"`), the actions it forbids and why, and a
`## Verification` section carrying the benchmark's own recovery probe, its
operator and threshold, and the two-consecutive-passes rule.
`scripts/tools/audit_runbook_coverage.py` grades every v2 scenario against this and
`scripts/tools/audit_runbook_controls.py` checks the grader by deleting properties
and asserting the score drops.

**Reason.** Measured on the live Notion corpus: retrieval was correct for
22/22 scenarios, and prescriptiveness was **0/22**. Every scenario reached the
right page and no page told the agent what to do — 22/22 carried no recovery
probe in a verification section, 20/22 omitted at least one prohibition, and
10/22 had no section that both prescribed an allowed action and ruled out the
forbidden one. Retrieval quality had been standing in for runbook quality. The
rewrites score 22/22 on the same grader.

**Consequences.**
- A runbook may only prescribe PromQL that `validate_promql` accepts. Writing
  these found two real gaps in the allowlist (`clamp_min`, and `le` inside
  `sum by (le)` read as an unknown metric) that made 16 of 22 recovery probes
  unrunnable by the agent. Fixed in `src/sre_agent/nl_query.py`.
- `DEFAULT_RUNBOOK_BRIEF_MAX_CHARS` rose 6000 → 9000. Every "Branch X —
  Action:" heading scores priority 0, so five branches spent the whole budget
  and Verification was dropped: the agent got every remediation option and
  lost the probe that says whether the one it chose worked.
- The corpus snapshot is committed at
  `evals/benchmarks/datasets/v2/runbook_corpus_snapshot.json` so the audit runs
  offline. It is a point-in-time copy; Notion is still the source of truth.

**Known limit.** The audit grades content, not routing. It finds a branch that
satisfies the scenario's contract; it cannot tell that the decision procedure
would send that fault to that branch, because several acting branches fit the
same contract by letter ("restart, and do not scale" suits a provider outage
and a bad deploy alike). One negative control stays deliberately blind for
this reason rather than being closed with a scenario-to-branch mapping that
would make the grader agree with its author by construction. Routing is
measured by running the agent.

**Rejected.** Scoring prescriptiveness document-wide. A first pass did, scored
22/22, and a negative control showed it was blind to a deleted prohibition:
Branch A's blanket "do not restart, scale or patch" satisfied the requirement
for every acting branch too. The honest score at that moment was 12/22. Any
grader tuned until it passes is measuring its own leniency.

## Match HolmesGPT's context controls at Sentinel's actual boundaries

**Decision.** Adopt HolmesGPT's source-side narrowing and measurable context
budgets, but implement them at Sentinel's boundaries: specialist briefs require
scoped time/label queries, aggregate/pattern tools before raw listings, and
small explicit limits; every ReAct model call records a `FitReport`, aggregated
per specialist under `metadata.context_fitting` and copied into the durable job
result on success or failure. Elision markers say that the raw bytes are in the
audit artifact but unavailable to the current model, and require a narrower
re-query rather than an inference from the preview.

**Reason.** HolmesGPT (reviewed at upstream commit
`3bd44edf04f9587c778ee8e9b244965190c40fdf`) controls context in layers:
server-side filters, optional result transforms, per-result limits, overflow to
local storage, and LLM compaction before an over-window call. Sentinel already
has the corresponding hard input reserve, per-result cap, deterministic
old-result shrinking/whole-turn dropping, pre-run LLM summary, and a lossless
content-addressed evidence artifact. Its missing pieces were prevention in the
tool instructions and durable counters proving how much fitting changed the
model view. Logs were also prompted to call nonexistent `search_logs(start=,
end=)` instead of `query_logs(start_time=, end_time=, limit=)`.

**Consequences.** The next validation can report model calls fitted, estimated
message tokens before/after/avoided, maxima, capped results, truncations, drops,
and any unfitted calls without storing prompts or evidence in telemetry. These
exclude tool schemas and provider framing and are conservative local estimates,
not provider-billed token counts; cost claims must still be reconciled with
`model_accounting`/Langfuse. The audit artifact remains lossless and graph state
remains untouched.

**Rejected alternatives.** Do not copy HolmesGPT's local temp-file pointer:
its shell tool can read that file, while Sentinel's remote MCP servers and
multiple API/worker processes cannot rely on a worker-local path, and the file
is not durable. Do not add an LLM summarization call inside every specialist
loop: it adds spend, changes evidence semantically, and breaks exact-prefix
prompt-cache reuse. Deterministic fitting is the cheaper live-loop control;
LLM summary remains limited to resumed/follow-up history before a run.

## Model-directed investigation reads are fail-closed and incident-scoped

**Decision.** A task-local `investigation_scope` marks specialist ReAct calls.
At the shared MCP wrapper, model-directed logs and metric reads require an
affected target plus an explicit alert window (30 minutes maximum); commit
history requires a three-hour maximum window; Kubernetes pod/event listings
require the affected workload/object; and runbook fallback search requires an
alert/runbook id or service plus incident type. Result counts are clamped.
Broad Kubernetes inventory tools and nonexistent runbook/GitHub convenience
tools are removed from specialist catalogs. Tenant namespace enforcement still
applies to every caller.

**Reason.** Prompt wording alone did not stop the Loki specialist from issuing
open-ended searches, and the old metrics prompt named tools that do not exist
while recommending canned values and placeholder services. The alert-aware
brief already supplies exact service/job/pod/namespace labels, alert time,
runbook procedure, and prior findings; the missing control was ensuring those
inputs reach actual tool arguments instead of an expensive discovery query.

**Consequences.** A rejected broad query is audited as `REFUSED` and returned
to the model so it can retry narrowly without cancelling sibling tool calls.
Deterministic runtime reads such as post-remediation alert verification are
not model searches and therefore bypass the investigation breadth gate while
retaining tenant isolation; this preserves process-death idempotency and Task
#40's post-clear no-remediation invariant. Specialist Langfuse observations
use stable role names (`logs_agent`, `metrics_agent`, and peers), so bounded
calls and refusals can be attributed to the right role. The 20k model-view cap
remains a backstop, not permission to issue a broad query.

**Rejected alternatives.** Prompt-only limits are advisory and already failed
in practice. Applying the incident-time gate globally would break deterministic
verification calls that intentionally query current alert state. Enforcing
only server defaults would also affect non-model callers and would not require
the model to use the alert target. A live cost delta remains unproven until the
changes are deployed and one bounded smoke trace is reconciled with provider
accounting/Langfuse.

## The GitHub server shapes commit payloads at the source, on a measured budget

**Decision.** `services/edge_mcp_servers/mcp_servers/github_real/payload.py` bounds what
a GitHub read puts in front of the model. `get_commit` returns up to 50 changed
file rows (filename, status, additions, deletions, changes) ordered
largest-change-first, reports the remainder, and spends whatever space those
rows and the message leave over — measured on the encoded JSON, not on raw
string lengths — on patch text
in that same order, 2000 characters per file, targeting 18,000 characters
against the 20,000-character model-view cap. `list_commits` passes
`since`/`until`/`path` to GitHub as server-side filters and refuses an
unparseable bound; `get_pull_request` caps the body at 4000 characters. Loss is
always reported: `files_omitted`, `patch_chars_omitted`, `files_scan_truncated`,
`body_truncated`, and a note repeating the elision contract.

**Reason.** `get_commit` had never returned a diff. It read
`commit.patch if hasattr(commit, "patch") else None`; `github.Commit.Commit`
has no such property and `GithubObject` defines no `__getattr__`, so the guard
always failed and the tool returned `"diff":null` on every call — while
`list(commit.files)`, the paginated read that does hold the patch text, was
walked and discarded except for its length. The tool description promised a
diff, so the model paid for a call that could not answer its question and
compensated with more calls. Simply returning `commit.files` would have been
the opposite failure: GitHub serves up to 3000 files per commit, and the
20,000-character head-and-tail elision downstream would then have kept the
alphabetically first and last hunks — an ordering unrelated to which file
caused the incident.

**Consequences.** Structure, not a byte offset, decides what survives: within
the 50-row reporting cap, a filename and its line counts are never sacrificed
for another file's diff text, because knowing that `config/limits.yaml`
changed is most of the finding. The budget is computed rather than constant
because a fixed one was wrong in both directions — a repo with deep paths
overflowed the cap (measured at 22,711 characters for 50 files) while a flat
one left tokens unspent. File
scanning stops at 300 entries (one page) and says so via
`files_changed_is_lower_bound`, bounding the edge server's own API cost.
`list_commits` keeps its substring `author` match client-side, since GitHub's
server-side `author` is exact, and bounds that scan at 200 commits.

**Rejected alternatives.** Leaving the payload unbounded and relying on the
downstream cap discards the ranking that makes the response useful. Returning
patches for the first N files by API order ranks by nothing. Raising the
20,000-character cap for this one tool reintroduces the quadratic transcript
cost the cap exists to control, since every ReAct iteration re-sends the
result.

## The scope gate reads the ToolCall's arguments, not the ToolCall

**Decision.** `wrap_tool_with_namespace_scope` splits a LangChain ToolCall
envelope (`{"name", "args", "id", "type"}`) from the arguments inside it,
enforces tenant scope and the bounded-read policy on the inner mapping, and
writes the result back into a copy of the envelope. A payload is treated as an
envelope on the canonical `type == "tool_call"` marker, or on an exact
`{name, args, id?, type?}` shape; anything else — including a tool whose own
schema has an `args` parameter — takes the unchanged flat path.
`enforce_tool_arguments` stays a pure function over a flat arguments mapping.

**Reason.** LangGraph's ToolNode invokes a tool with the whole ToolCall. The
wrapper passed that straight into the gate, so every lookup read the envelope:
`args.get("label_selector")` was `None` however well-formed the model's call
was. One smoke incident refused 126 of 142 tool calls, and three of four
specialists produced no evidence at all. The gate logic was never wrong — the
call site was one level off.

**Consequences.** Tenant isolation on the investigation read path was also not
being applied: for namespace-argument tools the read gate did not reject,
`args["namespace"] = effective` was written onto the envelope, so the tenant
namespace never reached the real arguments and `_scope_query` never scoped the
real `query`/`logql`. Both are closed by the same change. Regression tests now
drive a wrapped tool with a real ToolCall dict; the previous tests all called
`enforce_tool_arguments` with flat arguments, which is exactly why this
survived into production. The mutation path was checked and does not share the
exposure — it reads `action.parameters` on a typed planner object, and the
executor invokes tools with a flat dict.

**Rejected alternative.** Teaching `namespace_scope.py` to look one level down
when it sees an envelope. That spreads LangChain's call convention through a
module whose whole value is being a small, testable policy function, and it
would have to guess which level to write the enforced namespace back to.

## A recovery probe must return zero, not nothing, when a counter is absent

**Decision.** Counter-based recovery probes carry `or vector(0)` on the
numerator and, for ratios, inside `clamp_min` on the denominator —
parenthesised, since `/` binds tighter than `or`. Applied to 11 probe queries
across all three v2 splits, including the frozen holdout. `nl_query`'s
validator accepts that exact clause so the agent can run the same verification
query its runbook prescribes.

**Reason.** `sum(rate(http_errors_total{…}[5m]))` returns the empty vector
when the counter has no series yet — which is the normal state of an error
counter before a fault. The oracle then records "baseline returned no finite
scalar", sets `baseline_healthy=null`, ignores every later observation and
scores INVALID_SCENARIO. Measured against live Prometheus, 3 of 9 distinct
probes were empty at rest, covering 10 of 22 scenarios: those scenarios could
never be scored, whatever the agent did.

**Consequences.** `holdout.json` is `frozen: true` and was changed anyway.
The change is to the oracle's measuring instrument, not to scenario content,
labels or difficulty, and without it the holdout split cannot be scored at
all; the three `dataset.json` digests were regenerated. A probe whose series
is permanently absent now reads 0 instead of erroring — for probes with
`require_failure_observation`, the oracle still refuses to score a recovery it
never saw fail, but for the others a genuinely dead exporter would now look
healthy. That is a fault-injection question, not a probe question.

**Rejected alternative.** Allowing `or` generally in the query validator.
`_scope_query` injects the tenant namespace into the first selector block
only, so a general `or` would let a model-authored query union in a second,
unscoped selector. Only `or vector(<number>)` is stripped before the
identifier allow-list check; a bare `or` is still rejected.

## Autonomy is bootstrapped by the benchmark, not configured into it

**Decision.** Treat "every benchmark trial ends `awaiting_approval` and
`UNRESOLVED`" as the designed starting state of an uncalibrated system, and
plan the campaign as the thing that produces calibration — not as a run that is
blocked until calibration is configured.

**Reason.** Measured on incident `b13ce2c5` (2026-09-19), the chain is entirely
by-design. `severity_engine.py:311` escalates any severity one step while
`hypothesis_confidence_calibrated` is false, so `impact=0.73 × urgency=0.42 →
SEV2` became SEV1; `policy_gate` then returns `SEV1 (high severity) → human
approval required` for the mutating action; the graph raises
`GraphInterrupt(approval_required)`; `awaiting_approval` is in
`TERMINAL_APPLICATION_STATUSES`, so the harness stops; `resolved=false` means
`statistical_eval.py:229` *requires* `grader_status=NOT_APPLICABLE`.
`evals/benchmarks/confidence/README.md` states the premise plainly: no artifact is
committed, and `sre_bench.py` is the only sanctioned producer of the
`live_benchmark` records that may unlock autonomy. Nothing here is a bug to
fix; a shortcut around any link in it would be fabricated autonomy evidence,
which is exactly what the artifact contract exists to prevent.

**Consequences.** The campaign has a mandatory two-stage shape.
Stage 1 gathers *diagnosis* records — an unresolved trial still has both a
confidence and an outcome (0.86 / false on this run), so the diagnosis corpus
grows without autonomy. Stage 2 wires the resulting artifact via
`DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH` + `SENTINEL_CONFIG_FINGERPRINT`, which
stops the severity escalation and makes autonomous remediation reachable; only
then do *remediation* records exist, because `remediation_confidence_outcome`
comes from a grade criterion and the grade is NOT_APPLICABLE while unresolved.
`minimum_threshold_support` defaults to 40, so one trial per scenario (22)
cannot yield a non-null threshold — plan ≥2 trials per scenario for stage 1.
Two recording gaps compound this: `_record_confidence_observations` and the
trial record (and therefore `cost_usd`) both return early unless
`STATISTICAL_RECORDING`, so **smoke runs produce no calibration evidence at
all** and neither path has yet been exercised on the live stack.

**Rejected alternative.** Setting `REMEDIATION_CONFIDENCE_CALIBRATION_PATH` to
a hand-built artifact to unblock the campaign. `load_calibration_artifact`
recomputes the threshold curve from the bins and re-derives the selected point
from the recorded rule, rejecting any artifact whose threshold did not come
from an all-`live_benchmark` corpus — a hand-edited and re-digested artifact
fails to load. The contract is enforced, not advisory.


## The console shows approvals; Slack makes them

**Decision.** `POST /api/v1/incidents/{id}/approve`,
`POST /api/v1/incidents/{id}/mark-resolved` and
`POST /api/v1/incidents/{id}/remediation-gates/{gate}/decide` have no
dashboard caller **on purpose**. The console renders approval *state* and
routes the approval *action* to the incident's Slack thread. Do not add
approval buttons to the dashboard.

**Reason.** The standing product constraint is that Slack is the only
communication surface, and the dashboard already says so in words rather than
by omission: `clusters/[id]/incidents/[incidentId]/page.tsx:493` renders
`Approve or deny from the incident's Slack thread ("approve {gate}" / "deny
{gate}")` and `:558` renders `Reply "approve fix" in the incident's Slack
thread to run it.` Gate status is typed and displayed
(`PENDING | APPROVED | REJECTED | EXPIRED`), and `components/console/Rail.tsx:51`
carries an `awaitingApproval` badge on every page. One decision surface also
means one audit story: `approval_flow.py` reconciles the HTTP and Slack paths
deliberately so "mark-resolved and Slack's `mark resolved` / `acknowledge`
cannot drift" (`approval_flow.py:749`).

**Consequences.** Any endpoint-coverage audit will flag these three as
uncalled, and they will keep looking like the most alarming finding in the
report — an SRE console that cannot approve. That reading is wrong, and this
entry exists so the next audit does not spend its budget "fixing" it. A
path-level audit on 2026-09-20 found 42 of 56 v1 endpoints called; of the 14
without a caller, exactly one was a genuine gap (the emergency lock, now
wired). The other thirteen were this decision, the Alertmanager webhook, two
endpoints whose data already arrives embedded in a list response, and three
absences that are themselves deliberate — one of which,
`POST /clusters/{id}/jobs/trigger`, has since been removed outright (see "A job
may only be enqueued by a writer that stamps its handler").

**Rejected alternative.** Adding buttons "for parity", leaving Slack as one of
two ways to approve. Two writable surfaces for the same state transition means
two audit paths, two idempotency stories and a race at the gate, in exchange
for convenience on a flow whose whole point is that a human is deliberately in
it. The cheap half of the value — seeing what is waiting, and on what — is
already delivered read-only.

## Bound model turns structurally; do not pretend delayed cost is a hard gate

**Decision.** Each specialist may make six model turns by default and gets a
LangGraph recursion backstop derived from the same setting. The reflector may
request one reinvestigation round by default, reduced from three. When the last
allowed specialist turn requests another tool round, the stream is closed
before that tool or the following model call, the partial evidence is retained,
the exhausted limit is recorded, and cosmetic finding narration is skipped.
All three limits are clamped, configurable, and fingerprinted in the immutable
run manifest. The unused general `/api/v1/chat` route is removed; Slack incident
threads remain the sole user-facing conversation surface.

**Reason.** The 2026-09-21 smoke spent 85 of 97 model calls in specialists and
ran for 25.7 minutes despite scoped queries. The runaway dimension was repeated
ReAct and reflector cycles, not unbounded search parameters. A USD limit cannot
be a hard pre-call boundary because provider cost and token usage arrive only
after the call; throwing at that point can also turn finalization into a durable
job retry and spend more. Turns and graph rounds are knowable before spending.

**Consequences.** Defaults cap the initial four-specialist pass plus one full
four-specialist recheck at 48 specialist model calls, before fixed orchestration
and synthesis calls. A final answer on the sixth turn is accepted; only a sixth
turn that asks for more tools is stopped. `specialist_turn_budgets` makes limit
engagement visible in state, timeline evidence and traces. Raising a limit
changes the configuration fingerprint, so results across different limits are
not silently compared.

**Rejected alternative.** Wiring `RequestContext.remaining_budget` as a hard
dollar gate. It can be useful later for model-tier downgrade, but it cannot stop
the call that crosses the threshold, incomplete provider usage makes it
unavailable, and a raised exception near finalization risks a paid retry.


## One Slack message, one handler, one turn

**Decision.** Anything said inside a tracked war-room thread — @mention or
plain reply — runs the same body in `slack_bot.build_slack_app`
(`_route_war_room_text`): the approval commands get first refusal on the text,
then `war_room.route_thread_reply` takes it into
`mission_control.handle_incident_message`. The `app_mention` and `message`
handlers are thin entry points onto that one body, and `_claim_event`
deduplicates on `(channel, ts)` so exactly one of them acts.

**Reason.** The two events had separate bodies and drifted. `app_mention`
resolved the incident id from the registry and then handed it to
`nl_query.handle_chat_message`, the *ad hoc* dispatcher, which has no way to
touch a running investigation — it answered "Got it, I'll fold that into the
live investigation at the next checkpoint" and nothing did. On the product's
only communication surface, an @-mentioned `approve fix` was worse still: it
became small talk instead of an approval. Slack delivers a mention in a
channel the bot belongs to as *both* events with the same `(channel, ts)`, so
merging the bodies without a claim would have replaced one false answer with
two real agent turns for one sentence.

**Consequences.** The bot-echo guard had to move into the shared body with
it. `_on_thread_message` had always dropped `bot_id` messages and
`_on_mention` never needed to, because the worst a mention could produce was
an inert sentence; a mention that starts an investigation makes the agent's
own war-room posts — which do carry @mentions — a loop.

The claim table is bounded (512, oldest evicted) because
socket mode runs for weeks; a duplicate always lands milliseconds after its
original, so eviction cannot lose one. A message with no `ts` is never
deduplicated — a rare double reply beats a silently dropped question.
`format_reply`'s `steer` branch is now unreachable from Slack; it is kept, and
made truthful, for any other caller that hands the ad hoc dispatcher an
incident id. Correctness does not depend on believing anything about Slack's
fan-out: it holds whether one event arrives or both, in either order.

**Rejected alternative.** Having `app_mention` return early whenever the
thread is a war room, leaving the `message` handler as sole owner. Fewer
moving parts, but it silently assumes the workspace subscribes to
`message.channels`; where it does not, every @mention in an incident thread
would go unanswered, and the failure would look like the bot being down.


## A retrieval that returns something is not coverage

**Decision.** `ablation_coverage.py` reports two numbers per split, not one:
`skill_hit` (retrieval returned anything) and `signature_hit` (something it
returned shares the scenario's failure class, scored with `match_score` at the
same 0.5 floor `propose_skills` declares). The verdict line says when they
disagree. The retrieval floor itself is left alone.

**Reason.** The preflight exists so `no_memory` reporting NOT_DEMONSTRATED
cannot be confused with an empty corpus, and it was making exactly that
mistake one level down. `skill_hit` was `bool(skills)`, and the docstring
called the check "exact". On v2 it read COVERED 22/22. The honest numbers are
22/22 retrieve something, 12/22 retrieve their own failure class, and **4/22**
retrieve a skill matching class *and* service. Three of the five stored skills
are for services (`pdf-thumbnailer`, `ocr-extractor`, `thumb-worker`) that no
scenario touches, and `checkout-service` — the corpus's most common — has no
skill at all.

**Consequences.** The divergence has a single cause worth naming: on the
keyword path `propose_skills`' `threshold=0.5` is `match_score`'s scale, where
0.5 means "same failure class". `SemanticSkillStore._find_matching` compares
that same 0.5 against a Qdrant *cosine* similarity, which short signature
strings clear on embedding proximity alone. So the semantic path admits
skills the keyword path rejects, and the planner prompt can carry a skill
learned from an unrelated incident. Buying the `no_memory` arm on this corpus
buys 4 pairs that test whether relevant memory helps and 18 that test whether
irrelevant memory hurts — a real question, but not the one the arm is named
for.

**Rejected alternative.** Tightening the semantic floor now. It would change
what a paid run measures, and no data in the repo says what a defensible
cosine floor is; picking one by eye would substitute a guess for the
measurement the preflight is supposed to protect. Recorded as a blocker
instead.

## A runbook's query is extracted, not recalled

**Decision.** `src/sre_agent/runbook_queries.py` lifts PromQL out of runbook
markdown with regexes — no model call — and `build_specialist_task_brief`
quotes the result into the metrics specialist's user message, above an
instruction to run those expressions verbatim before exploring. The metrics
prompt's opening move changed from `get_golden_signals` to "the runbook's
queries, if the brief lists any".

**Reason.** The one graded trial failed because the Prometheus specialist
queried `http_request_duration_seconds_bucket` while the runbook named
`db_query_duration_seconds_bucket` in three places — the same metric the
recovery oracle probes. This was not inattention. `metrics_profile` holds
exactly one `latency_histogram` per cluster and `q_service_latency`
interpolates it, so `get_golden_signals` *cannot* return any other histogram;
the specialist opened with the tool the prompt told it to open with and got a
healthy service during a live incident. The prompt also promised a "metric
hint from the task brief" that no code produced — `grep -rn "metric_hint"`
matched the prompt line and nothing else.

**Consequences.** Extraction is deterministic and testable, so the failure
mode is a missing hint rather than a hallucinated metric, and it costs no
tokens to produce. Three rejections earn their complexity: templated queries
(`service="<service>"`) are dropped, because running one returns nothing and
manufactures the very "no data is a finding" conclusion the block teaches;
LogQL, shell and SQL are dropped; and the bare metric-name list is emitted
only when no query was found, because scraping prose otherwise yields
log-pattern strings like `db_pool_exhausted` that are not metrics. On the
real `examples/meridian/runbooks/high-latency.md` the first extracted query is the
`db_query_duration_seconds_bucket` p90 the oracle probes.

**Rejected alternative.** Telling the specialist to "read the runbook
carefully" or adding a metric-selection model call. Both buy turns, and the
user's constraint on this work was that cost must not rise. Also rejected:
widening `metrics_profile` to hold many histograms — a schema change that
would not have helped, since the runbook names the metric the profile would
still have to be taught.

## A wall-clock cut-off is a budget boundary, not a tool failure

**Decision.** When a specialist hits `SPECIALIST_TIMEOUT_SECONDS` mid-call,
its report is now the tool results already collected — `_partial_evidence_digest`
renders the last 12 `ToolMessage`s, 600 chars each — plus a note saying the
lane was cut off at a budget boundary. Before, the handler *replaced* the
response with an error string. A soft deadline reserving `min(30, timeout//3)`
seconds also declines to start a turn the clock cannot finish, narration is
skipped for any cut-short lane, and the reason (`turn_limit`, `soft_deadline`,
`timeout`) is recorded on the finding and the artifact.

**Reason.** The graded trial's supervisor was told there were no application
logs. There were: `query_logs` ran 31 times at a median of 0.1s. The 120s went
entirely to model latency (p90 26.1s, max 68.6s), and the handler threw away
every result the lane had already collected. The timeout was not raised —
raising it buys turns the clock currently cuts off, which is a real cost
increase.

**Consequences.** Latency now degrades the report instead of erasing it, and
a supervisor reasoning over a truncated lane can see that it is truncated.
The 30s headroom is the measured p90 of a specialist turn, so the deadline
usually costs nothing and occasionally forfeits one turn to save a whole
lane's evidence. Skipping narration on a cut-short lane also removes a model
call — narration was 6 calls for $0.0153 on the graded trial.

**Rejected alternative.** Raising the timeout, and retrying the killed call.
Both spend more to fix a reporting bug.

## The live transport had no output ceiling at all

**Decision.** `route_llm`'s LiteLLM branch now applies
`SREConstants.model.default_max_tokens` when the caller passes none, matching
the provider branch, and `_create_llm` sets a specialist turn's ceiling from
the new `SPECIALIST_MAX_OUTPUT_TOKENS` limit (default 3,000).

**Reason.** `default_max_tokens = 4096` is documented and applied through
`get_model_config` on the legacy provider path. The live path is LiteLLM —
every graded run shows `gen_ai.provider.name: litellm` — and it forwarded
kwargs unchanged, so a call with no explicit `max_tokens` had no ceiling.
One specialist turn in the graded trial emitted 6,402 tokens, 68.6s of that
lane's 120s at a measured 89 tok/s.

**Consequences.** A specialist turn is a report, not an essay: across the
trial's 85 turns output was p50 484, p75 857, p90 2,339, so a 3,000 ceiling
leaves nine turns in ten untouched and clips 7, removing 10,118 output tokens
(-$0.15 of $2.53) and ~114s. The limit is env-tunable and clamped to
[256, 16000].

**Rejected alternative.** Leaving the LiteLLM path uncapped and documenting
4,096 as the default anyway. The two transports disagreeing silently is how
this went unnoticed through every graded run so far.


## The runbook's own query is measured before the lane's first turn

**Decision.** `src/sre_agent/runbook_probe.py` evaluates the PromQL a runbook
names over `[alert - 5m, now]` and hands the first, peak and latest value to
the metrics lane inside its brief, before any model call. It runs through the
lane's *already bound* `get_metric_range`, so the probe inherits the tenant
namespace injection, the argument gate and the audit trail, and opens no
second MCP client. It is fail-soft: a probe that errors, times out or matches
nothing adds a note and changes nothing else.

**Reason.** Quoting the query to the model was not enough. The 2026-09-22
`inventory_slow_queries` trial ran the right expression at the wrong instant
— the alert timestamp, which the harness stamps at fault injection — so the
`rate(...[5m])` window was almost entirely pre-fault: 0.0221s against a 1.0s
threshold. The lane took the runbook's healthy branch and escalated a live
2.1s regression as "no action required". Choosing the evaluation instant for
a rate window is arithmetic, not judgement.

**Consequences.** The lane starts from a number instead of from a choice of
window, at the cost of one Prometheus range read per incident and no model
tokens. The brief also now forbids editing a runbook query's label matchers:
the same trial rewrote `job=` to `namespace`/`service`, which the series does
not carry, and read the empty result as "metric unavailable". The probe
window is 28 minutes so it stays inside `namespace_scope`'s 30-minute
investigation cap; a test asserts the probe's real arguments pass that gate.

**Rejected alternative.** Prompt-only — telling the model to query "now".
The brief had already told it the opposite ("do not query 'now'") and the
model obeyed; swapping one instruction for another leaves the outcome to
sampling, and this repo's precedent (`runbook_queries.py`) is to make the
deterministic part deterministic.

## The LangGraph step ceiling is sized to trip after the turn budget

**Decision.** A specialist's `recursion_limit` is
`specialist_model_turns * 3 + 2`, and `GraphRecursionError` is caught like
any other budget boundary: the lane reports its partial-evidence digest with
a note, and records `cut_short = "recursion_limit"`.

**Reason.** `create_react_agent` is built here with a `pre_model_hook`, which
is a real graph node, so one tool round costs three steps (hook, agent,
tools) and T model turns need `3T - 1`. The old backstop was `2T + 2` — 14
steps against the 17 a six-turn budget was meant to buy. The framework limit
therefore always fired before the graceful turn counter could, and it raised
instead of returning: the 2026-09-22 logs lane died twice reporting "no data"
after Loki had already answered.

**Consequences.** The explicit turn budget is the binding limit again and the
step ceiling is what it was meant to be, a backstop against a runaway graph.
Adding or removing a node in the specialist graph changes the multiplier;
`_REACT_STEPS_PER_TURN` names it in one place, with a test that pins it
against the configured turn count.

**Rejected alternative.** Raising the limit without catching the error. A
step ceiling is a budget, and every other budget in this lane — turns, wall
clock, call count — keeps the evidence already paid for.

## An environment ban belongs in the gate that can be appealed

**Decision.** `policy_engine` Rule 1 — block a PROD restart whose plan risk
score clears `POLICY_RESTART_RISK_THRESHOLD` — is deleted, and a comment in
its place records why it cannot come back. The intent stays in
`policy_gate.decide`, where it is already enforced from measured state:
telemetry must be known, severity must sit inside the autonomy band, and a
calibration artifact must clear the threshold.

**Reason.** The score Rule 1 judged was the planner's own `risk_level` string
mapped by `act_phase._plan_risk_score` (low 2.0, medium 5.0, high 8.0), so
only a plan that labelled itself "low" ever passed — the same untrusted-writer
inversion Rule 4 already records for `explicit_approval`. `calculate_risk_score`
also adds 0.5 per dangerous action, so proposing a restart raised the very
number used to judge it. Worse, an `evaluate_action` verdict is final:
`policy_gate.decide` returns BLOCKED before the approval ladder runs and
`_act_gate_node` builds the ACT report before looking up the approval, so no
human could authorize what the rule refused. The 2026-09-23 trial is the
evidence — a SEV3 restart with complete telemetry, blocked outright,
UNRESOLVED.

**Consequences.** A production restart now reaches REQUIRES_APPROVAL in
production *and* development. Two environment-spoofing tests used the PROD
restart block as their observable and would have passed either way after this;
both move to scale-to-0, which is still environment-discriminated, each with a
contrast case. Trial 6 took the new path end to end: `requires_approval` →
approve → four live remediations → oracle-verified recovery.

**Rejected alternative.** Keeping Rule 1 and reading a trustworthy risk signal
instead. There is no such signal at that layer — `evaluate_action` sees the
proposed action and the plan's self-description, and nothing else. The PROD
rollback floor (Rule 2b) is the documented precedent for moving a ban into
`decide`.

## A confidence observation is not a paired trial

**Decision.** `CONFIDENCE_RECORDING` is its own gate, defaulting on for any
live benchmark run. Outside a paired experiment the runner derives the config
fingerprint from the config that shapes the run and the pair id from a
per-process `RUN_ID`. Trial rows stay gated on the four `BENCH_*` experiment
vars.

**Reason.** Both records hung off `STATISTICAL_RECORDING`, and `BENCH_SCENARIOS`
raises rather than run beside it, so every single-scenario trial measured a
real (confidence, outcome) pair and dropped it unwritten: five live incidents,
zero samples, and a runtime that stays uncalibrated because the corpus it needs
is never written. The two records do not need the same identity — a trial row
means something only against its pair in another arm, which is what
`PAIR_SEED` protects; a confidence observation is one reliability point, and
its schema asks for a fingerprint and an id and nothing else.

**Consequences.** `reports/sre-bench-confidence.jsonl` accumulates across
ordinary single-scenario runs. The id must not repeat:
`load_confidence_records` rejects a duplicate
`(task, pair_id, config_fingerprint)` outright, and `make_pair_id` is
deterministic in a trial index that is 1 for every single-trial run, so a
repeat would not cost one sample — it would make the whole corpus unreadable.
The corpus groups by task, not scenario, so it must be spread across scenarios
before a threshold built from it means anything.

## The benchmark may play approver, and must say so

**Decision.** `BENCH_AUTO_APPROVE=1` lets the harness clear a pending approval
through the same two calls the dashboard makes — `GET /status` for the
`approval_request_id` and `action_hash`, then `POST /approve`. It is off by
default, bounded by `BENCH_AUTO_APPROVE_LIMIT` (3), counted into the grade
record's `harness_approvals`, and stamped onto the trial's
`failure_categories` as `harness_approved`.

**Reason.** `awaiting_approval` is terminal in `TERMINAL_APPLICATION_STATUSES`
and nothing ever cleared it, so a plan the gate correctly held for a human
ended the trial there and scored UNRESOLVED. On a production cluster
`policy_gate` holds every rollback and every uncalibrated mutation, which is
the behaviour we want; the benchmark could only ever punish it.

**Consequences.** The number a trial reports changes meaning with the flag on
— "can the agent fix this once authorized", not "unaided" — so the count is
recorded in both artifacts and an authorized arm can never be read back, or
diffed against an unaided one, as autonomous. This is not a DB bypass and not
a new code path: the graph still verifies the hash against the plan it runs,
and the Prometheus oracle still decides recovery on its own.

**Rejected alternative.** Treating `awaiting_approval` as a success. That
scores an unexecuted plan as a fix and would have made `false_resolved`
meaningless.


## An empty observability result must carry its own validity verdict

**Decision.** A Loki query that returns nothing now says why: whether the
selector was valid, and if not, which label or value does not exist.
`query_logs` reports `streams_matched` and, on any empty result,
`selector_valid` plus an `empty_result_reason`. An invalid selector is phrased
"INVALID QUERY, NOT EVIDENCE"; a valid selector over a quiet window is phrased
"This IS genuine evidence of silence". `analyze_log_patterns` forwards the
verdict instead of rebuilding a payload without it.

**Reason.** Loki answers a selector naming a label it does not have exactly the
way it answers a service that logged nothing: HTTP 200, zero streams. In trial 6
the Application Logs specialist ran five queries against `{app="..."}` — a label
this Loki does not index — and concluded, verbatim, "no evidence of a Loki tool
failure (no error/exception in the response, just `count: 0`) ... there's
nothing in the logs to contradict the Prometheus specialist's Branch A finding."
A query that could never have matched was promoted to a finding. This is #35's
class — a failure laundered into a success string — reached through a different
door: the call succeeded, and only the question was malformed.

**Consequences.** Every empty log result now carries either a warning that
forbids reading it as evidence, or a note confirming the silence is real. The
diagnosis costs one to three extra Loki calls, only on empty results, and is
best-effort: when the label index cannot be read it returns `selector_valid:
null` and still refuses to call the result silence. A tool may now spend a round
trip to establish that it has nothing useful to say.

**Rejected alternative.** Validating selectors before every query. That pays the
label-index cost on all calls to protect the minority that come back empty, and
still cannot separate a typo from a genuinely quiet service — only the empty
result raises the question worth answering.

## A runbook may not name telemetry the cluster does not have

**Decision.** `examples/meridian/runbooks/high-error-rate.md` Step 4 and
`downstream-dependency-failure.md` Step 2 now select on `service`, the label
this Loki indexes, rather than `app`, which it does not have.

**Reason.** Step 4's rule is "if any of the three returns lines → Branch D".
A selector on a nonexistent label returns zero lines unconditionally, so Branch
D — the database-connectivity remediation — was unreachable by construction. The
runbook did not merely fail to help; it routed every database-side fault to
Branch A or E. The agent was following the corpus correctly.

**Consequences.** The agent reads runbooks from Notion, not from the repo, so
the fix reached it only on republish (2026-09-23, four pages, 12 changed lines;
the other two drafts went out byte-identical). The live corpus now holds no
LogQL selector naming `app`, and `audit_runbook_coverage.py` scores 22/22
against the dump taken after the write rather than against the local drafts.
`evals/benchmarks/datasets/v{2,3}/runbook_corpus_snapshot.json` were regenerated from
that same dump. They are dumps of what Notion served, so they must always be
regenerated with `scripts/tools/dump_notion_runbook_corpus.py` and never hand-edited,
or the snapshot stops describing the corpus anyone read.

The `kubectl -l app=<service>` selectors elsewhere in the corpus were left
alone: pods do carry an `app` label. Only Loki lacks one, and only Loki's
silence was being read as evidence.

**Rejected alternative.** Teaching the tool to accept `app` as an alias for
`service`. That hides a corpus defect behind a tool that silently rewrites the
operator's query, and the next wrong label — on a cluster whose labels differ —
would get no such courtesy.

## A job may only be enqueued by a writer that stamps its handler

**Decision.** `POST /clusters/{id}/jobs/trigger`, `backend.crud.create_job` and
`schemas.JobCreate` are deleted. `sre_agent.job_store.enqueue_investigation`,
reached through `enqueue_and_kick`, is the only path that may write a row to
`jobs`.

**Reason.** `claim_jobs` selects on `status == PENDING AND job_type ==
INVESTIGATION AND cancel_requested_at IS NULL` and nothing else. `create_job`
wrote exactly that shape from caller-supplied fields, `job_type` defaulting to
`INVESTIGATION` and `payload` to `NULL`. The worker claimed the row, read
`handler` out of an empty payload in `execute_claimed_job`, raised
`DurableJobError("unsupported job handler: None")`, and retried to
`max_attempts` before dead-lettering — burning a lease each round. The console
never called it, so it never fired, but any authenticated org member could reach
it. Two components were each locally correct and jointly wrong: the claimer's
predicate and the writer's defaults agree only while every writer stamps a
handler.

**Consequences.** There is no hand-start for a job. An investigation with no
incident behind it has nothing to investigate, which was already the recorded
reason the console never wired the route; this closes it at the server instead
of relying on the console's restraint. `tests/test_durable_jobs.py` pins both
halves — `encode_investigation_payload` stamps `run_graph_background_saas`, and
`crud` exposes no `create_job` — so a future helper that writes a claimable row
without a handler fails the suite rather than the worker. `JobType` keeps its
import in `src/backend/schemas.py`, because `JobResponse` still uses it.

**Rejected alternative.** Keeping the route and refusing a payload-less
investigation. That leaves a mounted endpoint whose only valid input is a
payload no caller can construct — the handler name and idempotency key are
internal — so every honest request would be refused, and the helper behind it
would stay available to the next caller who skips the route.


## A cosine and a match_score are not the same number

**Decision.** Semantic skill recall admits on `_SEMANTIC_MATCH_FLOOR = 0.80`, a
constant measured over the v2 corpus, never on `find_matching`'s `threshold`. A
skill the keyword pass already admitted keeps its `match_score`; a cosine may
add a candidate but may not rescore or outrank one.

**Reason.** `match_score` is additive — 0.5 failure class + 0.3 service + 0.2
alert name — so `threshold=0.5` means exactly "same failure class".
`SemanticSkillStore._find_matching` tested Qdrant cosines against that same 0.5
and merged the two with `max()`, comparing scales that share nothing but the
0..1 range. Embedding all 22 v2 signatures with the production model and
scoring all 231 pairs shows the distance: pairs sharing a failure class score
0.851 at worst, pairs that do not score 0.764 at best, and a 0.5 cutoff admits
all 161 wrong-class pairs. That is the entire population, not a tail — the
filed example, `checkout_memory_leak_oom` retrieving
`latency-inventory-service`, was typical rather than unlucky.

**Consequences.** `evals/benchmarks/calibrate_semantic_floor.py` is that measurement,
kept runnable and free (local embedding model, no LLM). It exits non-zero if
the populations stop being separable or if the constant leaves the gap, so a
changed embedding model or `signature_text()` fails loudly instead of quietly
restoring the old behaviour. `find_matching` now returns two scales — keyword
hits first, ordered by `match_score`, then semantic-only hits by cosine — which
is why `ablation_coverage.py` recomputes `match_score` for `signature_hit`
instead of reading the score it was handed. That file's "semantic 6/6 against
keyword-only 3/6" coverage figure was produced by the defect; it is now marked
as pre-fix and has not been re-measured.

**Rejected alternative.** Scoring semantic candidates with `match_score` and
letting cosine only nominate them. `_skill_by_id` resolves Qdrant points
against the same in-memory dict the keyword pass already scanned, so the two
candidate sets are identical and the semantic path would collapse into a no-op
— deleting the feature rather than calibrating it. Its purpose is to reach a
skill whose wording matches when the failure-class keywords miss, and that
requires cosine to admit something `match_score` rejects.
