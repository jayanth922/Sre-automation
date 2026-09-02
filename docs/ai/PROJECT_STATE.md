# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Production-grade upgrade pass across the tracked concepts
(`decision-production-grade-upgrade` memory). Status as of 2026-09-02,
reconciled after discovering origin/master was 12 commits ahead of this
checkout (PRs #50/#51/#52, merged) plus 3 more local checkpoint commits —
see "Sync note" below.

**Done and merged to origin/master:**
- **Task #16 (live-fire validation)** — a genuine end-to-end happy path
  (investigate → plan → human-approve → live-execute → resolve) verified
  against real telemetry on Codespace `jubilant-space-invention-4vjq497q4x63jx5q`
  (`/workspaces/Sre-automation`, `kind-meridian` cluster). `restart_deployment`
  on `checkout-service` reported `EXECUTED`, independently confirmed at the
  k8s level (new ReplicaSet, timestamp matched). Root-cause fixes: restart-
  target parsing widened to free-form Planner text (`executor.py::_live_args`,
  DNS-1123-label regex), and `classify_live_response`'s payload parser fixed
  to recurse into the MCP SDK's real `{"type":"text","text":...}` content-
  block shape (was misreporting both successes and refusals as `ERROR`).
  Full root-cause writeup in `docs/ai/DECISIONS.md`. `.env` safety flags
  (`EXECUTOR_LIVE=false`, `SENTINEL_CLUSTER_ENVIRONMENT=production`) were
  reverted after the test and verified.
- **Model tiering + prompt caching** — `sre_agent/model_router.py` routes
  ROUTING/NARRATION/GREETING to Haiku 4.5, keeps SPECIALIST/AGGREGATION/
  REFLECTION/PLANNING on Sonnet 5; hand-rolled Anthropic prompt-caching
  helpers (`cache_control_marker`, `cached_system_message`, `cached_tools`)
  applied across `agent_nodes.py`, `graph_builder.py`, `supervisor.py`. This
  is model-**tier** routing within Anthropic, not yet cross-**provider**
  routing — see Next bounded task.
- **Payload compaction** — all 7 MCP servers + 5 prompt-builder sites emit
  compact JSON instead of `indent=2`, no quality impact.
- **Temporal-orchestrated code-fix verification** —
  `sre_agent/sandbox_workflow.py::CodeFixVerificationWorkflow` +
  `temporal_client.py` + `sandbox_worker.py`, committed `5822c07`, wired
  fire-and-forget into `graph_builder.py` (~line 290) whenever a proposed
  fix has a code-level action with sandbox params. Runs an unpatched
  baseline (must reproduce the failure signature), applies the patch, reruns,
  diffs logs → RESOLVED/REGRESSED/INCONCLUSIVE. This is the user's actual
  "temporal thing" ask (verify logs + resulting state are clean, not a
  rollback) — confirmed 2026-09-02, no further work needed. 8/10 tests pass
  without the optional `temporalio` extra installed; the other 2 just need
  `pip install sre-agent[temporal]` to spin up a `WorkflowEnvironment`.
- **Runbook RAG + NL-query production grade** — genuine vector search
  replacing keyword-only match (`sre_agent/runbook_index.py`, new
  `sre_runbooks_v1` Qdrant collection, queried by the Planner in
  `graph_builder.py` alongside existing MCP keyword search; incident-memory
  context added to `runbook_generator.py::generate_runbook_llm`), plus live
  Prometheus metric-catalog grounding and real-parser PromQL validation (two
  new MCP tools, `list_metric_names`/`validate_promql_syntax`, in
  `edge_mcp_servers/mcp_servers/prometheus_real/server.py`) with an LLM
  generation fallback for unmapped questions — all opt-in, default behavior
  unchanged. Committed `d7f9511`, pushed as **PR #53**
  (`feature/nlquery-rag-production-grade`, not yet merged).
- **Ad hoc Slack chat, genuine conversation** — the in-thread/incident-scoped
  conversation (`war_room.py` →
  `mission_control.handle_incident_message`) was already sophisticated and
  untouched. The real gap was the *other* path — a bare @mention or DM with
  no tracked incident — which silently hit `classify_chat_message`'s default
  `"steer"` fallback and got a misleading "I'll fold that into the live
  investigation" reply with no investigation to fold into.
  `nl_query.py::handle_chat_message` now branches `steer`-with-no-
  `incident_id` into `_handle_ad_hoc_chat`: a real LLM reply
  (`generate_chat_reply_llm`, `create_llm_with_fallback` pattern, same
  graceful-degradation-to-canned-reply on no provider as the rest of this
  file) with short-term multi-turn memory via `RedisStateStore.append_log`/
  `get_logs` keyed by a per-channel-per-user session key
  (`slack-chat:{channel}:{user}`, set in `slack_bot.py`'s `_on_mention`).
  `classify_chat_message`'s classification rules themselves are untouched
  (all 4 pre-existing assertions in `test_classify_chat_message_modes` still
  pass unmodified) — only the dispatch of the existing `"steer"` mode when
  there's no incident to steer changed. `slack_bot.py::format_reply` gained
  a `"chat"` mode branch; `process_mention`'s `session_key` param is
  additive/optional so old 2-arg test handlers keep working. 789/789 passed
  on this branch, 2 skipped (unchanged, `temporalio` optional extra) — 9 new
  tests across `test_nl_query.py`/`test_slack_bot.py`. Committed on
  `feature/slack-adhoc-chat-memory`, **PR #2** raised against `master`
  (independent of PR #53 — split via a 3-way `git merge-file` against
  `d7f9511`/`6a065ff` so the two PRs share no overlapping hunks and neither
  should conflict when merged in either order).

**Sync note (2026-09-02):** this checkout had drifted 12 commits behind
`origin/master` (this session's RAG/NL-query work was done on the stale
base). Resolved via `git fetch` + `git stash -u` + `git merge --ff-only`
+ `git stash pop`; only this file conflicted (code merged clean). All of
the above is now reconciled into one accurate picture.

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` env var gating
`execute_autonomous_live()`. `EXECUTOR_TOOL_MAP` (`executor.py:38-44`) routes
`action_type` → live MCP tool name. See `docs/ai/DECISIONS.md` for the full
rationale log (Task #16 root causes, cloud-dev-env-via-rsync convention).

## Active problem
None. **The entire expanded 4-item scope is now done**: model router
(tiering + genuine cross-provider), live-fire validation, Slack
conversational chat, Temporal code-fix verification. Re-checked
cross-provider claim directly against code (not just memory) before
starting new work — `model_router.py::_tier_provider`/
`_tier_model_override` (per-tier `MODEL_ROUTER_<TIER>_PROVIDER` override,
per-tier-per-provider model pin) plus `litellm_backend.py` (full LiteLLM
100+-provider backend, wired into `route_llm()`) were already committed in
`dadce79` — same "already done, just needed verification" pattern as the
Temporal item last checkpoint. 35/35 tests pass across
`test_model_router.py` + `test_litellm_backend.py`, including dedicated
cross-provider assertions (`test_per_tier_cross_provider_routing`,
`test_provider_specific_model_override_wins`). No code changed this pass —
verification only.

Per standing instruction (`decision-production-grade-upgrade` memory):
production-grade backend work is done; AIOpsLab domain benchmark and
UI/UX are next, previously deferred until backend was fully complete.

## Relevant files
- `sre_agent/model_router.py`, `agent_nodes.py`, `graph_builder.py`,
  `supervisor.py` — model tiering + caching.
- `sre_agent/sandbox_workflow.py`, `temporal_client.py`, `sandbox_worker.py`
  — Temporal code-fix verification (done).
- `sre_agent/nl_query.py`, `runbook_index.py`, `runbook_generator.py` —
  RAG/NL-query work, PR #53 (`feature/nlquery-rag-production-grade`).
- `sre_agent/nl_query.py::_handle_ad_hoc_chat`/`generate_chat_reply_llm`,
  `sre_agent/integrations/slack_bot.py` — ad hoc Slack chat memory,
  PR #2 (`feature/slack-adhoc-chat-memory`).
- `sre_agent/litellm_backend.py` — cross-provider router extension point.
- `docs/ai/DECISIONS.md` — durable technical decisions log, check before
  re-deriving root causes already documented there.

## Known blockers or risks
- `sre-langfuse-web`/`worker` crash-looping (ClickHouse `ON CLUSTER default`
  migration, no Zookeeper) — pre-existing, cosmetic only, doesn't affect the
  incident pipeline.
- GitHub Codespaces free tier is capped on core-hours — `gh codespace stop`
  when idle. Codespace `jubilant-space-invention-4vjq497q4x63jx5q` confirmed
  reachable and `Available` as of 2026-09-02.
- Approval requests expire ~30 min — see resolve→refire recipe below if
  re-testing live execution.

## Next bounded task
Expanded 4-item scope is complete and both remaining feature branches are
now committed and raised as PRs (#53 RAG/NL-query, PR #2 ad hoc Slack chat —
see "Relevant files" for branch names). Next milestone (confirm with user
before starting, per standing instruction): **AIOpsLab domain benchmark** or
**UI/UX improvements** — both previously deferred until backend was fully
done.

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
