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

**Phase F (2026-09-02, live-fire validated 2026-09-03):** closed a verified
gap — Phases B/C were structurally unreachable because nothing ever produced
the `patch`/`sandbox_*` params needed to start `IncidentRemediationWorkflow`,
and the planner had no code-fix action type. Now: planner can propose
`action_type="code_fix"` (target=repo/service, `parameters.description`=root
cause, no diff invented); both `_act_gate_node` detection blocks share a
`_sandbox_params_ready()` gate that tolerates a missing patch;
`IncidentRemediationWorkflow.run()`'s new first step, `generate_patch_activity`,
runs only when `patch` is empty — clones `GITHUB_REPO`, runs the pluggable
actor (`sre_agent/actor_runtime.py`, `AGENT_RUNTIME=local|hermes`, default
local) against it, takes `git diff` as the patch, parses agent-reported
`BASELINE_COMMAND`/`CANDIDATE_COMMAND` lines for the sandbox oracle, and
escalates (`PATCH_GENERATION_FAILED`) without opening gate 1 on any failure.
`HermesRuntime` now accepts `workdir`.

Live-fire validated on the Codespace against the real `jayanth922/meridian-shop`
repo (real Anthropic API calls, real git clone/diff, ambient Codespaces
`GITHUB_TOKEN`, read-only-confirmed): direct invocation of
`generate_patch_activity` produced a genuine `STATUS: GENERATED` with a sane,
minimal, comment-only diff and working `sh -c`-wrapped baseline/candidate
commands. Found and fixed 5 real bugs in the process (commit `f18062a`):
Anthropic extended-thinking responses (list-of-content-blocks, not a plain
string) breaking the LLM decider; `ActorResult.output` dropping
`TerminalAgent`'s final `summary`; the task prompt asking for a free-text
"final response" the decider's strict JSON schema can't produce (switched to
an echo-based marker instruction); `_parse_verification_commands` crashing on
`shlex.split()` of actor-generated shell text; and LLM decisions occasionally
embedding unescaped quotes inside a JSON string value (added a schema-aware
repair fallback).

**Full Temporal pipeline, both gates, live-fire validated 2026-09-03**
(commit `aeb9476`): stood up a Temporal dev server + `mcp-sandbox` MCP server
(new `edge_mcp_servers/mcp_servers/sandbox_real`, K8s namespace
`sentinel-sandbox`) on the Codespace, wired `IncidentRemediationWorkflow`
into the sandbox worker, and drove it end-to-end through both real approval
gates (`decide_start_fix` → `decide_raise_pr`) via Temporal signals against a
synthetic incident. Found and fixed 3 more real bugs (commit `aeb9476`):
`sandbox_workflow.py` called MCP tools by bare name (`"status"`) but
`sandbox_real` registers them `sandbox_`-prefixed; `sandbox_real/server.py`'s
K8s client never actually patched the kubeconfig to reach the Kind API
server by Docker-network hostname despite claiming parity with
`executor_real` (fixed + added missing `PyYAML` dependency);
`sandbox_gateway.py` assumed a bare string/dict MCP response but
`langchain_mcp_adapters` returns a list of content blocks (switched to the
existing `_structured_payload()` helper). With a synthetic incident whose
`generate_patch_activity` output didn't reproduce the fault, the log-diff
oracle correctly returned `INCONCLUSIVE` (fail-closed, not a bug); with
hand-set baseline/candidate commands designed to reproduce-then-fix the
signature, a real K8s sandbox Job pair ran to completion and the oracle
returned `RESOLVED`, opening gate 2 for the first time. Gate 2 was denied by
design (`decide_raise_pr(approved=False)`) — `raise_pr_activity`'s real
GitHub write access is confirmed only for reads against `meridian-shop` and
must not be exercised without separate, explicit user sign-off; every test
driver in this validation always denies gate 2 structurally.

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
- `sre_agent/incident_remediation_workflow.py` — Phase 5B/C: the two-gate
  Temporal workflow + `raise_pr_activity` (done); Phase F's
  `generate_patch_activity` (live-fire validated 2026-09-03); full workflow
  now live-fire validated through both gates 2026-09-03.
- `sre_agent/actor_runtime.py` — Phase F's pluggable actor
  (`AGENT_RUNTIME=local|hermes`), now takes `workdir`.
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
Both gates are now live-fire validated end-to-end (see above). The one
remaining piece before Phase E (cutover) is: confirm `GITHUB_TOKEN` write
access for `raise_pr_activity` against `meridian-shop` (only read access has
been confirmed so far — this needs a real PR-creation push, so **get
explicit sign-off before testing it**; every prior test driver has denied
gate 2 to structurally avoid triggering this). Open decisions (GitHub PR
repo scope/credentials, correlation adjacency source, Hermes safety review)
should be raised before Phase E relies on them. Phase D (`d7b7cb2`), Phase F
(`22cbb61`, `f18062a`), and the sandbox-pipeline fixes (`aeb9476`) are
committed locally, not yet pushed to `origin/master`.

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
