# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Production-grade upgrade pass across the previously-tracked concepts
(`decision-production-grade-upgrade` memory) is **complete and fully merged**
as of 2026-09-02. PR #53 (RAG/NL-query) and PR #54 (ad hoc Slack chat) are
both merged to `master` (`6ced925`).

**Phase 5 (in progress):** user requested a deterministic remediation
pipeline (Temporal-orchestrated, two manual approval gates per issue —
start-fix and raise-PR — per-issue isolated chat/PR, and concurrent-incident
correlation/bundling instead of one-root-cause assumption). Full plan,
code-verified gap analysis, and industry research in
`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md` — **read that file before
continuing this work**. **Phases A-F are implemented** (2026-09-02); see
"Relevant files" below for what each owns. A/B/C pushed to `origin/master`
(`4680faf`/`3375aca`/`b6d619c`); D (`d7b7cb2`) and F (`22cbb61`, bug fixes
`f18062a`) are committed locally, not yet pushed. Socket Mode audited:
`slack_sdk`'s socket client auto-reconnects independent of wait length — the
"~30min expiry" is our own `APPROVAL_TTL_MINUTES` gate TTL, not a transport
issue.

**Phase F (2026-09-02, live-fire validated 2026-09-03, commit `f18062a`):**
closed a verified gap — Phases B/C were structurally unreachable because
nothing produced the `patch`/`sandbox_*` params `IncidentRemediationWorkflow`
needs. Planner can now propose `action_type="code_fix"`;
`generate_patch_activity` (new first step, runs only when `patch` is empty)
clones `GITHUB_REPO`, runs the deterministic actor
(`sre_agent/actor_runtime.py::LocalTerminalRuntime`), takes `git
diff` as the patch, and parses agent-reported `BASELINE_COMMAND`/
`CANDIDATE_COMMAND` lines for the sandbox oracle. Live-fire validated
against the real `jayanth922/meridian-shop` repo; 5 real bugs found and
fixed in the process (detail in commit `f18062a`, not restated here).

**Full Temporal pipeline, both gates, live-fire validated 2026-09-03**
(commit `aeb9476`): stood up a Temporal dev server + `mcp-sandbox` MCP
server (new `edge_mcp_servers/mcp_servers/sandbox_real`) on the Codespace
and drove `IncidentRemediationWorkflow` end-to-end through both real
approval gates via Temporal signals against a synthetic incident — a real
K8s sandbox Job pair ran to completion, the log-diff oracle returned
`RESOLVED`, gate 2 opened for the first time. 3 more real bugs found/fixed
(detail in commit `aeb9476`). Gate 2 was denied by design — `raise_pr_activity`'s
real GitHub write access is confirmed only for reads against `meridian-shop`
and must not be exercised without separate, explicit user sign-off.

**Phase 5E (2026-09-03, implemented and live-fire validated, not yet pushed):** extended
`IncidentRemediationWorkflow` per user request into a bounded retry loop with
on-call handoff, replacing the old single-shot generate→gate1→verify→gate2
flow:
- `run()` now loops up to `RETRY_MAX_ATTEMPTS=3` end-to-end attempts
  (generate patch → verify). Attempt 1 keeps exact prior behavior (including
  the test-only `params.patch` bypass); every retry (2, 3) always regenerates
  via the actor runtime, fed `retry_context` — a running text log of why
  prior attempts failed (`self._attempt_history`) — so the actor tries
  something different instead of resubmitting the same diff.
- Each retry requires its own fresh human approval: new `retry_fix` gate/
  signal (`decide_retry_fix`), independent of gate 1 (`start_fix`).
- When attempts are exhausted (or a retry is denied/expires),
  `_close_out()` opens a new `close_incident` gate carrying the full
  multi-attempt failure history as its PENDING detail (surfaces to Slack
  automatically — no new Slack code needed, see below) and waits for
  `decide_close_incident`. Approval → `mark_incident_needs_manual_review_activity`
  sets `Incident.status = REMEDIATION_FAILED` and the workflow returns
  `CLOSED_NEEDS_MANUAL_REVIEW` (a normal terminal return; Temporal marks the
  workflow COMPLETED, nothing further to clean up).
- New `POST /{incident_id}/mark-resolved` (`mission_control.py`) lets
  on-call manually confirm a `REMEDIATION_FAILED` (or any) incident as
  `RESOLVED` once they've verified it themselves outside the pipeline.
- `approval_flow.py`'s `_GATE_SIGNAL_NAME` and `war_room.py`'s
  `GATE_COMMAND_RE` extended with `retry_fix`/`close_incident` — both were
  already fully generic over the gate string, so no other logic changed.
  Slack gate-open notifications for these two new gates work today for free
  via the existing `war_room_service.py::maybe_open_war_room` /
  `forward_events` mechanism (it forwards every `event_type="act"` event,
  which `emit_gate_event_activity` always uses) — this was verified to
  already work for the *existing* two gates too; no new Slack-specific code
  was needed anywhere in this pass.
- Dashboard (`page.tsx`): removed the chat compose box and the gate
  approve/deny buttons (`sendMessage`/`decideGate` and their state) per
  user's explicit "conversation and complex querying should happen from
  Slack" instruction — kept the read-only conversation feed and gate
  status/label display (the "AI traceability" the user asked to retain).
  The old pre-Phase-5 single-gate `approve()`/`awaitingApproval` UI is a
  different, unrelated mechanism and was deliberately left untouched.

**Phase 5E live-fire validated 2026-09-03 (test-11, workflow id
`e2e-remediation-test-11`):** drove the new retry loop end-to-end on the
Codespace against the real Temporal server. Attempt 1 forced a `REGRESSED`
verdict (hand-set baseline/candidate commands, distinct oracle branch from
test-9's `INCONCLUSIVE` and test-10's `RESOLVED`); `AWAITING_RETRY_1` opened,
`decide_retry_fix(True)` approved it. Attempt 2 ran for real (real
`generate_patch_activity` against `meridian-shop`) and came back
`GAVE_UP` (actor step-budget exhausted); `AWAITING_RETRY_2` opened,
`decide_retry_fix(False)` denied it — exercising the "denied retry" branch
of `_maybe_retry` (attempt-exhaustion branch not separately exercised).
`AWAITING_CLOSE_INCIDENT` opened next with the full two-attempt failure
history in its `detail` string; `decide_close_incident(True)` closed it.
Final result: `RemediationVerdict(status='CLOSED_NEEDS_MANUAL_REVIEW', ...)`,
confirmed directly in Postgres: `Incident.status = remediation_failed`.
`POST /{id}/mark-resolved` was not live HTTP-tested (judged sufficient by
code review — mirrors an existing, identically-authenticated endpoint). All
5 touched files (4 Python + `page.tsx`) previously passed `py_compile`/
`tsc --noEmit`. Committed (`b0c352e`) and pushed to `origin/master`.

**Real GitHub PR-write path confirmed end-to-end, 2026-09-03 (test-18,
workflow id `e2e-remediation-test-18`):** user gave explicit sign-off
("yes, go ahead") to exercise `raise_pr_activity`'s live write against
`jayanth922/meridian-shop`. First real attempt (test-17, real valid patch
against current `README.md`) got past `git apply` and the local commit but
failed at `git push` — root cause: `gh repo clone` authenticates the clone
itself but leaves no credential helper for a subsequent plain `git push`.
Fixed in `edge_mcp_servers/mcp_servers/github_exec/server.py`'s
`create_fix_pr` by calling `gh auth setup-git` once before cloning
(idempotent, registers gh's credential helper for git). Re-ran as test-18
with the same patch: `RemediationVerdict(status='PR_CREATED',
pr_url='https://github.com/jayanth922/meridian-shop/pull/1',
verification_status='RESOLVED')` — a real PR was created. PR #1 carries a
"safe to close" marker comment (trivial `README.md` addition); not yet
closed, awaiting user decision. Local commit for the `server.py` fix not
yet made as of this note.

**Done and merged to origin/master** (pre-Phase-5 milestone; see
`docs/ai/DECISIONS.md` and git log for full detail, not restated here): Task
#16 live-fire validation; model tiering/prompt caching + cross-provider
routing; Temporal code-fix verification (`CodeFixVerificationWorkflow`, now
reused as Phase 5's sandbox-verify child); runbook RAG/NL-query (PR #53); ad
hoc Slack chat memory (PR #54).

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` env var gating
`execute_autonomous_live()`. `EXECUTOR_TOOL_MAP` (`executor.py:38-44`) routes
`action_type` → live MCP tool name. See `docs/ai/DECISIONS.md` for the full
rationale log (Task #16 root causes, cloud-dev-env-via-rsync convention).

## Active problem
None outside Phase 5 (tracked above). Per standing instruction
(`decision-production-grade-upgrade` memory): pre-Phase-5 production-grade
backend work is done; AIOpsLab domain benchmark and UI/UX remain deferred
until Phase 5 completes.

## Relevant files
- `sre_agent/incident_correlation.py`, `sre_agent/api/v1/alerts.py::_record_correlation_shadow`,
  `backend/crud.py::list_active_incidents_for_cluster` — Phase 5 correlation
  gate (Phase A, done, shadow mode).
- `sre_agent/service_topology.py` (`build_adjacency_map`, `get_adjacency_map`) —
  k8s-label-inferred service adjacency for the correlation gate (implemented
  2026-09-03, see `docs/ai/DECISIONS.md`): groups by
  `app.kubernetes.io/part-of` label + resolves NetworkPolicy ingress/egress
  peers via label-selector matching against Service selectors. Wired into
  `_record_correlation_shadow` (non-fatal — any k8s/MCP/Redis failure falls
  back to `adjacency=None`, same as before this feature). Cached per cluster
  via `RedisStateStore.set_topology_cache`/`get_topology_cache`
  (`sre_agent/redis_state_store.py`, key `sre_agent:topology:{cluster_id}`,
  5 min TTL). Fetched over the `k8s` MCP server via the new
  `list_network_policies` tool (`edge_mcp_servers/mcp_servers/k8s_real/server.py`
  — new `get_networking_v1_api`, `_format_network_policy_entry`,
  `handle_list_network_policies`) and `sre_agent/executor.py::build_k8s_tool_caller`
  (new, mirrors the existing per-server tool-caller wrappers). Tests:
  `tests/test_service_topology.py` (pure `build_adjacency_map` cases).
  **Live-fire validated 2026-09-03** against the Codespace's `kind-meridian`
  cluster (containers patched via `docker cp`, not via `git pull` — the
  codespace's git tree is 39+ commits behind `origin/master` with its own
  uncommitted drift, so validation went straight into the running
  `mcp-k8s`/`sre-agent-api` containers): confirmed `list_network_policies`
  returns real data end-to-end, confirmed the no-signal path (`{}`, matches
  the cluster's actual empty label/policy state), then confirmed the
  positive-signal path by temporarily labelling `checkout-service`/
  `payment-service` with a shared `part-of` value — got the correct
  bidirectional adjacency map back, cache round-tripped through Redis (2nd
  call issued zero MCP requests), labels removed afterward. Found and fixed
  one real bug: `get_adjacency_map`'s `_parsed()` helper assumed the MCP tool
  caller returns a bare string or dict, but `build_mcp_tool_caller` (via
  `langchain_mcp_adapters`) actually returns a list of content blocks
  (`[{"type": "text", "text": "<json>"}]`) — unfixed, this would have
  silently degraded to `adjacency=None` in production even when the
  underlying k8s calls succeeded. The same latent pattern (`json.loads(raw)
  if isinstance(raw, str) else raw`) also exists in `sre_agent/nl_query.py`
  (~lines 234, 258) — not fixed here (out of scope), flagged for a future
  pass. The codespace's container images were also missing the
  `langchain-mcp-adapters` and `redis` pip packages entirely (both declared
  in `pyproject.toml` but absent from the running images) — installed live
  for validation only; not persisted, so a container rebuild from the baked
  image will need them reinstalled until the image itself is rebuilt.
- `sre_agent/incident_remediation_workflow.py` — Phase 5B/C: the two-gate
  Temporal workflow + `raise_pr_activity` (done); Phase F's
  `generate_patch_activity` (live-fire validated 2026-09-03); full workflow
  live-fire validated through both gates 2026-09-03; Phase 5E's bounded
  retry loop (`retry_fix`/`close_incident` gates, `_maybe_retry`/
  `_close_out`, `mark_incident_needs_manual_review_activity`) implemented and
  live-fire validated 2026-09-03 (test-11).
- `sre_agent/api/v1/mission_control.py::mark_incident_resolved` — Phase 5E's
  manual on-call "mark resolved" endpoint (`POST /{id}/mark-resolved`).
- `sre_agent/actor_runtime.py` — Phase F's deterministic actor
  (`LocalTerminalRuntime`, Temporal-orchestrated, no third-party backend),
  takes `workdir`.
- `edge_mcp_servers/mcp_servers/sandbox_real/` — Phase 5B's sandbox-verify
  MCP server (K8s Job lifecycle for `CodeFixVerificationWorkflow`); live-fire
  validated 2026-09-03, 3 bugs fixed (commit `aeb9476`).
- `sre_agent/sandbox_gateway.py`, `sre_agent/sandbox_workflow.py` — sandbox
  authorization boundary + verify workflow; response-parsing and tool-name
  bugs fixed (commit `aeb9476`).
- `sre_agent/approval_flow.py` (`create_or_reuse_pending_gate_approval`,
  `decide_gate_approval`, `expire_gate_approval`), `sre_agent/api/v1/remediation_gates.py`,
  `backend/models.py::RemediationGateApproval`, migration `e4f5a6b7c8d9` —
  Phase 5B gate persistence + API (done).
- `edge_mcp_servers/mcp_servers/github_exec/server.py` — `create_revert_pr`
  (reverts) and `create_fix_pr` (arbitrary patches, Phase 5C, done); real
  PR-write path live-fire validated 2026-09-03 (test-18, PR #1 on
  `meridian-shop`), `gh auth setup-git` credential-helper fix applied.
- `sre_agent/graph_builder.py::_act_gate_node` — deterministic-pipeline
  detection/deferral + `IncidentRemediationWorkflow` trigger (Phase 5B/C
  wiring, done).
- `dashboard/app/(dashboard)/clusters/[id]/incidents/[incidentId]/page.tsx`
  — two-gate approval panel (`loadGates`/`decideGate`, Phase 5D, done).
- `sre_agent/war_room.py` (`parse_gate_command`, `route_gate_command`),
  `sre_agent/integrations/slack_bot.py::_slack_user_email`,
  `sre_agent/approval_flow.py::decide_and_signal_gate`/`find_latest_pending_gate`
  — Slack gate-decision commands (Phase 5D, done).
- `docs/ai/DECISIONS.md` — durable technical decisions log, check before
  re-deriving root causes already documented there.
- `tests/test_mcp_auth.py::test_compose_ports_are_loopback_only_and_require_token`
  — hardcodes the MCP service count in `docker-compose.yaml` (loopback ports
  + `MCP_SERVICE_TOKEN` requirement); bump both counts whenever a new
  `mcp-*` service is added (fixed 2026-09-03, `5ed525d`, after `mcp-sandbox`
  broke it at 8 vs. hardcoded 7).

## Known blockers or risks
- `sre-langfuse-web`/`worker` crash-looping (ClickHouse `ON CLUSTER default`
  migration, no Zookeeper) — pre-existing, cosmetic only, doesn't affect the
  incident pipeline.
- GitHub Codespaces free tier is capped on core-hours — `gh codespace stop`
  when idle. Codespace `jubilant-space-invention-4vjq497q4x63jx5q` confirmed
  reachable and `Available` as of 2026-09-02.
- Approval requests (both the single-gate `ApprovalRequest` and the new
  `RemediationGateApproval` rows) expire ~30 min (`APPROVAL_TTL_MINUTES`) —
  see resolve→refire recipe below if re-testing live execution. This is a
  business TTL, not a Slack transport limitation (confirmed during Phase 5D
  — Socket Mode auto-reconnects on its own well within any wait length).

## Next bounded task
`edge_mcp_servers/mcp_servers/github_exec/server.py`'s `gh auth setup-git`
fix is committed and pushed (`f106378`); throwaway PR #1 on `meridian-shop`
is closed. CI was also found broken on `master` (had been red for the prior
3 pushes, since `mcp-sandbox` was added to `docker-compose.yaml` without
updating `tests/test_mcp_auth.py`'s hardcoded port/token counts — a stale
assertion, not a real security gap); fixed and pushed (`5ed525d`), CI
confirmed green again on `master`.

Phase E cutover is done (see below) — both of its preconditions (real
PR-write path confirmed end-to-end via test-18; correlation adjacency
implemented, live-fire validated, and committed — see
`sre_agent/service_topology.py` in Relevant files above) are satisfied.

**`nl_query.py` MCP content-block unwrap bug — fixed (2026-09-03).**
`fetch_metric_catalog`/`validate_promql_syntax_live` did
`json.loads(raw) if isinstance(raw, str) else raw`, missing the same unwrap
`service_topology.py` needed: MCP tool_callers built via
`build_mcp_tool_caller` return a list of content blocks
(`[{"type": "text", "text": "<json>"}]`), not a bare string/dict, so `data`
became that list and `data.get(...)` — silently caught by the surrounding
`except Exception`, failing open/closed rather than crashing, but the live
metric-catalog/live-syntax-check features were quietly no-ops for any real
MCP caller. Added `_parsed_tool_result()` (same unwrap-then-`json.loads`
helper as `service_topology.py`'s `_parsed`) and routed both functions
through it. Not yet wired to a real caller in `sre_agent/` (only
`tests/test_nl_query.py` exercises this pipeline today), so this was a
latent bug, not an active outage. New regression tests
(`test_fetch_metric_catalog_unwraps_mcp_content_block_list`,
`test_validate_promql_syntax_live_unwraps_mcp_content_block_list`) exercise
the real content-block shape. Full suite green (853 passed, 3 skipped);
ruff/mypy findings confirmed pre-existing via `git stash` diff (1 ruff, 39
mypy, unchanged counts before/after).

**Hermes actor backend fully removed (2026-09-03, see `docs/ai/DECISIONS.md`
"Hermes removal").** After the 2026-09-03 safety review left
`AGENT_RUNTIME=hermes` blocked (undocumented toolset surface, no filesystem
sandbox — see the earlier "Hermes safety review" entry), user determined the
existing `LocalTerminalRuntime` (first-party, Temporal-orchestrated,
already live-fire validated) is sufficient and the third-party integration
adds risk without functional gain. Removed: `HermesRuntime` class,
`AGENT_RUNTIME` backend-selection branching, the `hermes` extra in
`pyproject.toml`/`uv.lock`, and the Hermes-specific tests in
`tests/test_actor_runtime.py`. `get_agent_runtime()` now always returns
`LocalTerminalRuntime` — no env-var selection, one backend. Full suite green
(848 passed, 3 skipped — the delta is exactly the 7 removed Hermes tests),
lint/mypy clean (pre-existing unrelated findings confirmed via `git stash`
diff, not introduced here).

**Execution-trace dashboard view — done (2026-09-03).** Added step-level
`event_type="trace_step"` timeline events (source/step/status + optional
diff/logs) via `emit_trace_step_event()`/`truncate_for_timeline()`
(`sre_agent/incident_timeline.py`), wired into `generate_patch_activity`
(`clone_repo` STARTED, `run_actor` STARTED, terminal `generate_patch`
SUCCEEDED/FAILED via a `_finish()` closure — `sre_agent/incident_remediation_workflow.py`)
and `_run_sandbox_stage` (`sandbox_{stage}` STARTED before provisioning,
terminal REFUSED/SUCCEEDED/FAILED with truncated logs after fetch —
`sre_agent/sandbox_workflow.py`). These are supplementary to the existing
gate PENDING/APPROVED bubbles and the final verdict bubble, not a
replacement — they fill the previously-silent gap while a fix is actually
being generated/verified. Frontend: incident page's event loop
(`dashboard/app/(dashboard)/clusters/[id]/incidents/[incidentId]/page.tsx`)
got a `trace_step` render branch (status-colored node, `.sx-badge` pill,
collapsible `<details>` diff/log block via new `.sx-trace`/`.sx-tracesum`
CSS in `dashboard/app/console.css`); `tsc --noEmit` and `eslint` clean (6
pre-existing unrelated `no-unescaped-entities` errors confirmed via `git
stash` diff). Backend: full suite green (848 passed, 3 skipped, unchanged),
new pure-function tests for `truncate_for_timeline` in
`tests/test_timeline_crud.py`; ruff/mypy findings confirmed pre-existing via
`git stash` diff. Not live-fire tested against the Codespace cluster yet —
the emission points are additive (new events only, no control-flow change),
so the existing live-fire validation of `generate_patch_activity`/
`_run_sandbox_stage` themselves still applies; a full live-fire pass through
the dashboard is the natural next check whenever an incident runs there
next.

**Phase E cutover — done (2026-09-03).** Retired the old single-gate
live-in-LangGraph auto-execute path for code-fix actions, per the plan's
condition ("once Phases A-D have run clean against real incidents" — met by
test-11/test-18 above). Fixed a real gap in `_act_gate_node`
(`sre_agent/graph_builder.py`): the "deterministic pipeline detection" block
only deferred a detected code-fix action's `decision` to
`DEFERRED_TO_DETERMINISTIC_PIPELINE` when `temporal_enabled() and
_sandbox_params_ready(...)` were both true — if Temporal was disabled or
sandbox params weren't ready yet, the action's `decision` was left
unchanged and remained eligible for `execute_autonomous_live()` below,
bypassing the two-gate deterministic pipeline entirely. Deferral is now
unconditional on detecting *any* code-fix action; readiness still gates only
whether `IncidentRemediationWorkflow` actually starts (the separate,
untouched "Code-fix verification" block reports `INCONCLUSIVE` instead when
unready) — it never again gates whether the old live path runs.
Non-code-fix actions (restart/scale/rollback/etc.) are unaffected and
correctly remain on the old live path, matching Phase 5's code-fix-only
scope. Hardened the fix's own import of `DEFERRED_TO_DETERMINISTIC_PIPELINE`
(from `incident_remediation_workflow.py`, which imports the optional
`temporalio` SDK at module level — "only the worker container needs it, not
the API image" per `pyproject.toml`) with an `ImportError` fallback to the
same literal string, so `_act_gate_node` can't crash on an API image that
never installed the `temporal` extra. New regression test
`tests/test_act_integration.py::test_code_fix_action_deferred_even_when_temporal_disabled`
exercises exactly that worst case (Temporal disabled, `temporalio` not
installed). Full suite green (851 passed, 3 skipped); ruff/mypy findings
confirmed pre-existing via `git stash` diff (identical counts before/after:
35 ruff, 190 mypy).

## Resolve→refire recipe (for re-testing checkout-service fault, on the
Codespace's `kind-meridian` cluster)
1. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0`
2. Poll `kubectl exec -n meridian deploy/prometheus -- wget -qO-
   http://localhost:9090/api/v1/alerts` until no `alertname` (~2-5 min).
3. Confirm incident status flips to `resolved` in Postgres.
4. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0.6`
   to fire a genuinely new incident (dedup matches by title on non-resolved
   incidents only). Planner's proposed actions are non-deterministic across
   identical fault runs.
