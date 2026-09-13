# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) is complete and live-fire
validated — see `docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md` and git log.
Current focus: **Slack-only communication robustness** (standing design rule:
"Slack is the only method of all types of communication, so it should be
robust") plus live-fire testing of varied incident types (Task list #4/#5).

Just landed (2026-09-12/13, commits `abe0609`, `22e76fd`, `82fa810`,
`f81e241`, pushed to `origin/master`):
- Approval auto-renew: a still-`PENDING` Slack approval that has gone stale
  (past `expires_at`) is renewed rather than refused, since the approver was
  already freshly re-verified for this decide call.
- Root-caused and fixed "Approved, but the remediation failed to resume":
  container's `execution_context.py` was stale, missing
  `org_langfuse_credentials()` already called by deployed code. Synced 6
  files (execution_context/agent_runtime/checkpointer/tracing/
  litellm_backend/model_router).
- Per-org Langfuse tracing (`Organization.langfuse_*`, migration
  `d4e5f6a7b8c9`, `POST /organization/langfuse`, dashboard UI in
  `team/page.tsx`), per-cluster LLM API keys via ContextVar
  (`model_router.bind_api_key`), and a relative (not absolute) model-tier
  ladder in `model_router.py`.
- **Live execution status fix**: `approval_flow.py`'s post-approval
  remediation resume now streams (`graph.astream`) instead of a single
  blocking `ainvoke`, writing per-node status to Redis
  (`redis_state_store`, keyed by incident id) the same way the initial
  investigation phase already did — previously the remediation phase wrote
  no live telemetry at all. `narrative.py`/`mission_control.py` thread that
  Redis state into the Slack/dashboard follow-up narrator so "what's
  happening right now" answers from current state, not the last DB
  checkpoint (which only moves at summary/status-transition boundaries).
- Slack command robustness: `war_room.py` normalizes command text
  (strips @mentions, markdown emphasis, trailing punctuation) and detects
  approval-intent near-misses ("approved", "lgtm", "go ahead") to ask for
  the exact command instead of silently falling through to the LLM chat
  path. `war_room_service.py` resolves the Slack bot token from whichever
  org has one installed instead of requiring a global env var.
- Fixed `_is_chat_only_message` misclassifying substantive questions
  ("what changed recently after the deploy?") as chat-only merely for
  ending in "?" — was skipping real investigations.
- **Not yet live-verified**: the live-status fix has not been observed
  working end-to-end (needs a fresh incident through investigate → approve
  → remediation-resume; the one incident that existed pre-fix already
  finished its resume under the old code path).
- All existing demo incidents removed from the Codespace's Postgres
  (`incident_timeline_events` deleted, `jobs.incident_id` nulled,
  `incidents` deleted — cascades handled `approval_requests`/
  `run_manifests`/`remediation_gate_approvals`) for a clean slate.

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` gating
`execute_autonomous_live()`. Slack is the sole approval/communication
channel by design — every fix in that area must preserve "robust under real
typing" (mentions, markdown, punctuation, near-miss phrasing) rather than
requiring an exact literal string. See `docs/ai/DECISIONS.md` for durable
rationale.

## Completed or verified work
Pre-Phase-5 through Phase 5 cutover, MCP cross-stack Docker-network fix,
Temporal sandbox workflow, per-org Langfuse tracing, per-cluster LLM keys,
model-tier ladder, and this session's Slack-robustness/live-status fixes —
all live-fire validated and pushed to `origin/master`. Full narrative history
superseded by git log; do not re-derive root causes already fixed there.

## Active problem
Live-fire testing varied incident types (Task #4 software, #5 hardware) on
Codespace `cuddly-winner-659v67gv695hrxjw`'s `kind-meridian`/k3s cluster.
Incident data was just cleared to a clean slate. Next real incident should be
used to confirm the live-execution-status Slack fix actually surfaces the
current remediation step mid-run.

## Relevant files
- `sre_agent/approval_flow.py` — approval CAS + graph resume (now streaming).
- `sre_agent/redis_state_store.py` — live per-node execution state, keyed by
  incident id, TTL 3600s.
- `sre_agent/api/v1/mission_control.py` — shared dashboard/Slack message
  handler (`handle_incident_message`, `_is_chat_only_message`,
  `_build_chat_reply`).
- `sre_agent/narrative.py` — LLM narrator for chat replies/follow-ups.
- `sre_agent/war_room.py`, `sre_agent/war_room_service.py`,
  `sre_agent/integrations/slack_bot.py` — Slack command parsing/routing.
- `docs/ai/DECISIONS.md` — durable technical decisions.

## Verification commands and latest results
`uv run pytest -q` → 882 passed, 3 skipped, 1 pre-existing unrelated failure
(`test_approval_flow.py::test_graph_and_api_enforce_verified_synchronous_
resume`, fails even at a clean stash — asserts stale text against
`dashboard/.../incidents/[incidentId]/page.tsx`, not yet root-caused, not
touched by recent work). `git checkout -- uv.lock` after any `uv run
pytest` (it mutates the lockfile as a side effect).

## Known blockers or risks
- GitHub Codespaces free tier is capped on core-hours — stop when idle
  (`gh codespace stop -c <name>`); a stopped codespace has previously
  disappeared within ~1 day (cause unconfirmed).
- Approval requests expire ~30 min (`APPROVAL_TTL_MINUTES`) — auto-renewed
  now while still `PENDING`, per the fix above.
- `docker exec sre-agent-api python ...` fails (no venv) — use
  `docker exec -w /app sre-agent-api uv run python ...`.
- The container is image-baked, not volume-mounted: any code change needs
  `gh codespace cp -e` → `docker cp` into the container → restart.
- The pre-existing `test_graph_and_api_enforce_verified_synchronous_resume`
  failure (see above) blocks nothing currently but should eventually be
  root-caused or deleted if the dashboard page it checks was legitimately
  refactored.

## Next bounded task
Trigger a fresh SLO-breaching incident on `checkout-service`, approve its
fix via Slack, and mid-remediation ask "what's happening right now" in the
same thread to confirm the live-status fix actually surfaces the current
node — this is the one part of this session's work not yet live-verified.
