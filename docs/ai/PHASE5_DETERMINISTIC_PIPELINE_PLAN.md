# Phase 5 — Deterministic Remediation Pipeline

Status: **draft, awaiting user sign-off**. No implementation started.
Source: user request, 2026-09-02 (verbatim requirements below).

## User's requirements (verbatim intent, paraphrased into a checklist)

1. Deterministic, not AI-improvised: detect → gather evidence → check GitHub
   for code context → if found, take the AI-generated fix → apply it via a
   Temporal workflow → re-check the system → if green (or the anomaly's log
   signature is gone) → raise a PR for on-call to review.
2. Two separate manual-approval gates per issue: (a) approval to start the
   fix in Temporal, (b) approval to raise the PR. Every issue, both gates,
   always human-gated.
3. Real-time chat with no connection drop-outs, two-directional, on both
   Slack and the dashboard.
4. Each issue handled and PR'd separately — no merging unrelated issues.
5. When multiple issues arise concurrently, don't assume one root cause.
   Determine whether they're correlated (bundle) or independent (handle
   separately, in parallel via isolated sub-agents/workflows) — "check how
   industry does this."

## What already exists (verified in code, 2026-09-02)

| Piece | File | Verdict |
|---|---|---|
| Sandbox fix verification (baseline vs. patched log diff) | `sre_agent/sandbox_workflow.py::CodeFixVerificationWorkflow` | **Reuse as-is.** Already exactly the "apply in Temporal, check again, RESOLVED/REGRESSED/INCONCLUSIVE" oracle requirement 1 asks for. Currently triggered fire-and-forget *after* live execution (`graph_builder.py:287-352`) as a confirmation, not *before* it as a gate — needs to move earlier in the flow. |
| Dashboard real-time transport | `dashboard/lib/useLiveStream.ts` | **Reuse as-is.** Exponential-backoff reconnect, fresh ticket per attempt, cursor-based replay on reconnect, client dedup by event id. Already satisfies requirement 3's dashboard half. |
| Slack transport | `sre_agent/integrations/slack_bot.py` (Socket Mode, `AsyncSocketModeHandler`) | **Reuse, audit only.** Library-level reconnect exists; not yet stress-tested against a 30+ minute approval wait (current known blocker: approval requests expire ~30 min — see `PROJECT_STATE.md`). |
| Per-issue chat isolation | `sre_agent/mission_control.py`, `war_room.py` | **Reuse.** Incident-scoped threading already exists; keying everything (thread, workflow, PR) off one `workflow_id` per bundle gets requirement 4 for free. |
| Exact-title incident dedup | `sre_agent/api/v1/alerts.py:241`, `crud.find_duplicate_incident` | Exists but is **not** correlation — exact title match only, same cluster. No root-cause grouping across different alert titles. |
| GitHub PR creation | `edge_mcp_servers/mcp_servers/github_exec/server.py::create_revert_pr` | **Partial.** A PR-opening tool exists (`gh pr revert`, dry-run-by-default, typed CREATED/MANUAL_REQUIRED/ERROR statuses, guardrailed) but only for reverting an existing merged commit/PR — there is no tool to open a PR carrying an arbitrary AI-generated patch/diff. Phase C is "add a `create_fix_pr` tool next to `create_revert_pr`," not a from-scratch build. |
| Concurrent-issue correlation/bundling | `sre_agent/incident_correlation.py` | **Done (Phase A, shadow mode) — see below.** |
| Two-gate approval (start-fix / raise-PR) | *(none)* | **Missing.** Today there is one approval that directly triggers live execution (`mission_control.py::approve_incident_action`). |

**Net-new work is: correlation engine, PR creation, the two-gate signal
workflow, and reordering the existing sandbox verify to run before any live
action.** The chat/transport layer is not being rebuilt, only wired to the
two new gates and audited.

## Industry research (requirement 5)

- **Alert correlation** (PagerDuty Intelligent Alert Grouping, Datadog
  Watchdog): combine (a) time-window proximity, (b) service-topology/
  dependency adjacency, (c) learned or textual similarity between alert
  signatures — not a single signal. [PagerDuty docs](https://support.pagerduty.com/main/docs/intelligent-alert-grouping),
  [Datadog Event Management](https://www.datadoghq.com/blog/datadog-event-management/).
- **Multi-agent orchestration**: the production-standard shape is a
  **supervisor** that classifies/routes work, handing each independent unit
  to its own **isolated child agent/workflow**, control returning to the
  supervisor when that child finishes. [LangGraph supervisor vs swarm](https://focused.io/lab/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture),
  [AI Agents for Incident Management](https://www.augmentcode.com/guides/ai-agents-incident-management).
- **Mapping onto our stack**: Temporal already gives us isolated child
  workflows for free. The natural design is a light `CorrelationSupervisor`
  step that decides bundle-vs-separate *before* spawning, then one
  `IncidentRemediationWorkflow` per bundle — not per raw alert.

## Target architecture

```
alert fires ──▶ CorrelationSupervisor (new, lightweight, not an LLM-per-alert
                 decision — see below)
                   │
                   ├─ within N minutes + same cluster/dependent service +
                   │  hypothesis-similarity above threshold?
                   │      yes ──▶ attach to existing open bundle's workflow
                   │      no  ──▶ spawn new IncidentRemediationWorkflow
                   ▼
      IncidentRemediationWorkflow (new, Temporal, one per bundle)
        1. gather_evidence        (reuse investigator/reflector as activity)
        2. github_code_context    (new activity: does repo own the failing
                                    path? if yes, LLM proposes ONE patch)
        3. ── Approval Gate 1 ──  workflow.wait_condition on a signal:
                                   "start fix in Temporal" (Slack + dashboard,
                                   identical payload, same workflow_id)
        4. CodeFixVerificationWorkflow (EXISTING, unchanged, as a child)
                                   baseline vs candidate sandbox log diff
        5. RESOLVED?  ── no ──▶ escalate to on-call (existing `escalate`
                     │          action), no PR, workflow ends
                     yes
                     ▼
        6. ── Approval Gate 2 ──  signal: "raise PR"
        7. create_pull_request    (new activity, net-new capability)
        8. done — PR linked back to the incident/thread
```

Correlation is deliberately **not** another live LLM call per alert (that
would reintroduce the same improvisation problem requirement 1 objects to).
It's a bounded, deterministic scoring function: time window + adjacency +
similarity ≥ threshold. LLM involvement stays confined to step 2 (propose
one patch) and only within a workflow already scoped to one bundle.

## Phasing (avoid one giant PR; each phase is independently testable)

- **Phase A — Correlation engine. DONE (2026-09-02), shadow mode.**
  `sre_agent/incident_correlation.py::correlate` — pure, deterministic,
  zero LLM calls in the hot path (per requirement 1: correlation itself must
  not be another improvised AI decision). Scores concurrent open incidents in
  the same cluster on three signals, same shape PagerDuty/Datadog use: time-
  window proximity, service-topology adjacency (via an optional injected
  adjacency map; falls back to same-service-name match extracted from the
  existing `[{service}] {alertname}` title convention), and Jaccard text
  similarity between title+description. 15 unit tests, no DB dependency
  (`tests/test_incident_correlation.py`, same pattern as
  `alert_resolution.py`/`test_alert_resolution.py`). Wired into
  `sre_agent/api/v1/alerts.py`'s webhook handler as `_record_correlation_shadow`,
  called non-fatally right after `crud.create_incident` — for every new
  incident, scores it against `crud.list_active_incidents_for_cluster` (new
  helper) and, if it would bundle, writes an observational
  `correlation_shadow` timeline event (what it would bundle with, score,
  reasons) via the existing `create_incident_timeline_event`. **Genuinely
  shadow-mode**: dedup, investigation dispatch, and execution are completely
  unchanged — nothing acts on the correlation result yet. Full suite: 834
  passed / 2 skipped (was 820/2 before this phase). No service-dependency
  topology data source exists yet, so the adjacency map is an optional
  parameter with no live caller-side source wired in — needs a decision
  (see Open decisions) before Phase B can rely on adjacency, same-service
  matching already works standalone.
- **Phase B — `IncidentRemediationWorkflow` skeleton + both approval
  signals. DONE (2026-09-02).** `sre_agent/incident_remediation_workflow.py`
  — a Temporal workflow with hard `decide_start_fix`/`decide_raise_pr` signal
  gates (first-write-wins, timeout-bounded, no default-approve path), running
  the existing `CodeFixVerificationWorkflow` as an unmodified child workflow
  *between* the two gates (Phase 5A's reordering: sandbox-verify before any
  live/PR action, not after). `graph_builder.py::_act_gate_node` detects a
  code-fix action with complete sandbox params up front; when Temporal is
  enabled and the detection is ready, that one action's report is deferred
  (mutated to sentinel `DEFERRED_TO_DETERMINISTIC_PIPELINE`, outside the
  known `AutonomyDecision` set, so `act_phase.execute_autonomous_live`'s
  existing skip-logic naturally leaves it alone) and `IncidentRemediationWorkflow`
  is started instead of the old single-gate `CodeFixVerificationWorkflow`
  fire-and-forget; unready/temporal-disabled cases fall through to the prior
  INCONCLUSIVE messaging unchanged. Two API endpoints
  (`sre_agent/api/v1/remediation_gates.py`, admin-gated) list and CAS-decide
  each gate's `RemediationGateApproval` DB row (new model + migration
  `e4f5a6b7c8d9`), then call `temporal_client.signal_workflow()`.
- **Phase C — GitHub PR creation. DONE (2026-09-02), folded into Phase B.**
  `edge_mcp_servers/mcp_servers/github_exec/server.py::create_fix_pr` —
  clone → branch → apply patch → commit → push → PR (via `gh`/`git` CLI),
  typed CREATED/MANUAL_REQUIRED/ERROR outcomes mirroring `create_revert_pr`,
  guardrailed (allow-list + repo allow-list + param completeness). Invoked
  by `IncidentRemediationWorkflow`'s `raise_pr_activity` only after both
  gates and a `RESOLVED` sandbox verdict.
- **Phase D — Wire Slack + dashboard to the two new gates. DONE (2026-09-02).**
  Dashboard: `dashboard/.../incidents/[incidentId]/page.tsx` gained a
  `GateApproval` fetch (`GET /incidents/{id}/remediation-gates`, polled
  alongside the existing `loadStatus`) and a `decideGate()` action
  (`POST .../remediation-gates/{gate_approval_id}/decide`), rendered as a new
  "Deterministic pipeline" panel above the existing single-gate remedy panel
  — same admin-gated button pattern as `approve()`, extended to two gates
  and an explicit deny. Slack: `war_room.py` gained `parse_gate_command`
  (pure, parses "approve start-fix" / "deny raise-pr" replies) and
  `route_gate_command`, which resolves the replying Slack user's email
  (`slack_bot.py::_slack_user_email`, via `users_info` — the only identity
  bridge available, since there's no per-user Slack↔app mapping, only the
  org-level OAuth bot token from Phase 4) to a `User` row, requires
  `role == ADMIN` the same as the dashboard's `require_admin`, then decides
  the gate and signals the workflow through a new shared
  `approval_flow.decide_and_signal_gate` (factored out of
  `remediation_gates.py`'s endpoint so the two transports can't drift on the
  gate→signal-name mapping). `slack_bot.py`'s thread-message handler tries
  `parse_gate_command` first and falls back to the existing free-text
  `route_thread_reply` when the text isn't a gate command. No Block Kit —
  text commands only, matching this codebase's existing plain-text Slack
  design (`format_reply`, `format_event_for_slack`). The *outbound* half
  (gate PENDING/APPROVED/DENIED/EXPIRED reaching Slack) needed no new code:
  `emit_gate_event_activity` already writes an `event_type="act"` timeline
  event, and `"act"` was already in `war_room._SURFACED`, so
  `forward_events` was already carrying it into the war-room thread.
  Socket Mode audit: `slack_sdk.socket_mode.aiohttp.SocketModeClient`
  defaults to `auto_reconnect_enabled=True` with a 5s ping interval —
  confirmed via the installed package's constructor signature. The
  transport self-heals independent of wait length; the "~30min expiry"
  risk is entirely our own `APPROVAL_TTL_MINUTES` business TTL (default 30,
  `approval_flow.approval_ttl`), unrelated to the socket connection. No
  transport fix needed; this distinction is now documented so it isn't
  mistaken for a connection bug later. 6 new tests
  (`tests/test_war_room.py`, `tests/test_slack_bot.py`); full suite 841
  passed / 3 skipped (was 835/3).
- **Phase E — Cutover.** Retire the old single-gate live-in-LangGraph
  auto-execute path once Phase A–D have run clean against 1–2 real incidents
  on the Codespace, matching this repo's existing live-fire validation
  convention (Task #16).
- **Phase F — AI patch generation (actor-runtime). Implemented, committed
  (`22cbb61`), and live-fire validated (2026-09-03, `f18062a`)** — see
  `docs/ai/PROJECT_STATE.md`'s "Phase F" paragraph for current status. Root
  cause: user asked who executes the
  AI-generated patch in the sandbox today, and code inspection found the
  honest answer is **no one, because the pipeline never gets that far**.
  `graph_builder.py:359-379` reads `patch`/`diff` and four
  `sandbox_*` parameters off the triggering action before it will even start
  `IncidentRemediationWorkflow`, but grepping the whole tree turned up **zero
  production write sites** for any of those five keys — they're read-only.
  Compounding that, `RemediationAction.action_type`
  (`agent_state.py:123-125`, a Pydantic `Literal` structured-output schema)
  has no code-fix intent at all — only `restart/scale/rollback/config_change/
  patch/escalate/revert_commit`, where `"patch"` there means a live K8s
  resource patch (`policy_gate.py:68`), not a git diff. So today the
  deterministic pipeline's fix/verify machine (Phases B/C, fully built and
  tested) is structurally unreachable outside unit tests that hand-construct
  the params directly. Separately, `sre_agent/actor_runtime.py`'s
  `HermesRuntime`/`LocalTerminalRuntime` (the "Hermes agent" integration
  audited this session) is real, correctly-wired code but has exactly one
  caller in the whole tree (`terminal_agent.py`'s standalone CLI, itself only
  invoked by `benchmarks/terminal_bench_adapter.py`) — confirmed dead from
  the product's perspective, and `scripts/check_module_reachability.py`
  self-admits this by explicitly whitelisting `actor_runtime`/`terminal_agent`
  as `EXPERIMENTAL`. Phase F closes both gaps at once: use the actor runtime
  to generate the missing patch, upstream of gate 1, so gate 1 finally has a
  real diff to show instead of never firing.

  Planned changes:
  1. `agent_state.py` — add `"code_fix"` to `RemediationAction.action_type`'s
     `Literal`; nudge the planner prompt (`supervisor.py`) to propose it
     (target = repo/service, parameters = root-cause description) when the
     fix is source-level, not infra-level. No diff at this stage — intent
     only, same as it proposes `revert_commit` today.
  2. `incident_remediation_workflow.py` — loosen `IncidentRemediationInput.patch`
     /`baseline_command`/`candidate_command` to optional. Add
     `generate_patch_activity` as a new first step, run only when `patch` is
     empty, before the existing `emit_gate_event_activity("start_fix",
     PENDING)`: shallow-clones `repo` into a scratch dir keyed by
     `workflow_id` (mirrors `sandbox_workflow.py::_job_name`'s pattern,
     cleaned up in the same `finally`), then calls
     `actor_runtime.get_agent_runtime(workdir=clone_dir)` with a task built
     from `remediation_plan.hypothesis` plus the incident's existing
     evidence/failure signature (no new evidence plumbing — already gathered
     upstream). Success: `git diff` in the clone becomes `patch`; the agent
     also proposes `candidate_command`/`baseline_command`. `GAVE_UP`/`ERROR`:
     workflow ends terminal (`PATCH_GENERATION_FAILED`), a timeline event is
     emitted, gate 1 never opens — nothing to approve, matching today's
     INCONCLUSIVE-and-stop pattern.
  3. `failure_signature` and `runner_image` stay **deterministic, not
     agent-generated** — `failure_signature` from the incident's already-
     detected alert/error signature, `runner_image` from a new small
     per-repo/per-cluster config lookup (a shell agent can't build a
     container image). Only the diff and repro commands come from the actor.
  4. `graph_builder.py:373`'s all-five-params gate loosens to require only
     `runner_image` + `failure_signature` + `repo` up front; `patch`/commands
     become optional-if-missing, filled in by step 2 inside the workflow.
  5. `actor_runtime.py::HermesRuntime._build_agent()` — fix: it never passes
     `workdir`/cwd to `AIAgent(...)` today, so it would run wherever the
     process happens to be instead of the incident's repo clone. Required
     fix regardless of which backend is selected.
  6. Backend default stays `AGENT_RUNTIME=local` (`LocalTerminalRuntime`) for
     this step even after Hermes is wired up — it already has an audited,
     tested deny-list (`terminal_agent.py::_DENY_PATTERNS`) and a workdir
     scope; Hermes's own tool-safety hasn't been reviewed from inside this
     codebase, so it stays opt-in per cluster until it has been.
  7. No change to gate semantics, dashboard, or Slack contracts — gate 1
     keeps the same two-gate shape, now with a real diff attached instead of
     an unreachable branch.

## Open decisions for the user (not yet answered — flag before Phase B/C)

- GitHub PR creation scope: one target repo (`meridian-shop`) or
  generalized to whatever repo a cluster is bound to?
- PR creation credentials: GitHub App install vs. a scoped PAT already
  available in this environment?
- Correlation thresholds (time window, adjacency source — is there an
  existing service-dependency graph, or does one need to be built/inferred
  from k8s labels?) — likely needs a short calibration pass against real
  incident data, not guessed up front.
- Phase F: **resolved 2026-09-03** — `LocalTerminalRuntime` (audited
  deny-list) is the actor, full stop; `HermesRuntime`/`AGENT_RUNTIME=hermes`
  was removed rather than reviewed further (see `docs/ai/DECISIONS.md`
  "Hermes removal"). `get_agent_runtime()` no longer takes a backend name.

## Next bounded task

Phase F's `generate_patch_activity` is live-fire validated in isolation
(direct invocation, real repo, real LLM) — confirmed `STATUS: GENERATED`
with a sane diff and working baseline/candidate commands, and 5 real bugs
found/fixed along the way (see `docs/ai/PROJECT_STATE.md`). Not yet
exercised: the full Temporal-orchestrated `IncidentRemediationWorkflow`
end-to-end through both real gates (no Temporal server exists on the
Codespace yet — needs standing one up), and `raise_pr_activity`'s write
access to `meridian-shop` (only read access is confirmed). Phase E (cutover)
follows once those are clean. Open decisions above (GitHub PR repo scope, PR
credentials, correlation thresholds/adjacency source, Hermes safety review)
are still unanswered and should be raised before either phase relies on
them.
