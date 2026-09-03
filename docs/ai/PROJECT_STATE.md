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
continuing this work**. **Phase A (correlation), B (workflow + gates), C (PR
creation), and D (Slack + dashboard gate wiring) are done** (2026-09-02).
A/B/C are committed and pushed to `origin/master`
(`4680faf`/`3375aca`/`b6d619c`); D is implemented and test-covered, not yet
committed. `IncidentRemediationWorkflow`
(`sre_agent/incident_remediation_workflow.py`) runs the existing
`CodeFixVerificationWorkflow` as a child *between* its two hard gates;
`graph_builder.py::_act_gate_node` detects a sandbox-ready code-fix action
and defers it (sentinel decision value, see that file's detection block) to
the new workflow instead of the old single-gate path; two admin-gated API
endpoints (`sre_agent/api/v1/remediation_gates.py`) decide each gate and
signal the running workflow — the dashboard's incident page now polls and
acts on those endpoints, and Slack replies of the form "approve start-fix"/
"deny raise-pr" inside a tracked war-room thread do the same, authorizing
off the replying Slack user's email resolved to an org-admin `User` row
(`war_room.py::route_gate_command`, `slack_bot.py::_slack_user_email`).
Socket Mode audited: `slack_sdk`'s socket client auto-reconnects (5s ping,
`auto_reconnect_enabled=True` by default) independent of wait length — the
known "~30min expiry" is our own `APPROVAL_TTL_MINUTES` gate TTL, not a
transport issue. Full suite: 841 passed / 3 skipped. Remaining: E (cutover,
retiring the old single-gate live path).

**Done and merged to origin/master** (pre-Phase-5 milestone; see
`docs/ai/DECISIONS.md` and git log for full detail, not restated here):
- Task #16 — live-fire validation, real telemetry, `docs/ai/DECISIONS.md`
  has the root-cause writeup.
- Model tiering + prompt caching (`model_router.py`) and cross-provider
  routing (`litellm_backend.py`) — both done, verified against code.
- Temporal code-fix verification (`sandbox_workflow.py::CodeFixVerificationWorkflow`)
  — this is Phase 5's sandbox-verify step, now reused as a child workflow.
- Runbook RAG/NL-query production grade — **PR #53**, merged.
- Ad hoc Slack chat memory (`nl_query.py::_handle_ad_hoc_chat`) — **PR #54**,
  merged.

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
- `sre_agent/incident_remediation_workflow.py` — Phase 5B/C: the two-gate
  Temporal workflow + `raise_pr_activity` (done).
- `sre_agent/approval_flow.py` (`create_or_reuse_pending_gate_approval`,
  `decide_gate_approval`, `expire_gate_approval`), `sre_agent/api/v1/remediation_gates.py`,
  `backend/models.py::RemediationGateApproval`, migration `e4f5a6b7c8d9` —
  Phase 5B gate persistence + API (done).
- `edge_mcp_servers/mcp_servers/github_exec/server.py` — `create_revert_pr`
  (reverts) and `create_fix_pr` (arbitrary patches, Phase 5C, done).
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
Phase 5, Phase E: cutover — retire the old single-gate live-in-LangGraph
auto-execute path once Phase A–D have run clean against 1–2 real incidents
on the Codespace (Task #16's live-fire validation convention). Open
decisions still unanswered (GitHub PR repo scope/credentials, correlation
adjacency source) should be raised before Phase E relies on them — see
`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`. Phase D changes are
implemented and test-covered but not yet committed — commit (separately
from A/B/C, which are already on `origin/master`) before starting Phase E.

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
