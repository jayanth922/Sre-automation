# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Task #16: **DONE.** Verified a genuine end-to-end happy path (investigate →
plan → human-approve → live-execute → resolve) against real telemetry on
the GitHub Codespace `jubilant-space-invention-4vjq497q4x63jx5q`
(`/workspaces/Sre-automation`). Confirmed via `restart_deployment` on
`checkout-service` reported `EXECUTED`, independently verified at the k8s
level (new ReplicaSet rolled out, timestamp matching the approval).
`.env` safety flags reverted and verified (see below). Next: decide with
user whether to commit the session's fixes to git.

## Completed or verified work
1. **MCP networking fix** — widened 7 `edge_mcp_servers/docker-compose.yaml`
   port bindings for `host.docker.internal` connectivity. Confirmed working.
2. **Loop-abort fix** (`sre_agent/act_phase.py` ~596-630) — one action's
   `MutationRejected` no longer aborts the whole batch. Confirmed live.
3. **Restart-target parsing fix (widened)** — Planner targets for `restart`
   actions are free-form, not just `"<deployment>:<sub-resource>"`
   (observed live: full descriptive phrases like `"checkout-service pods
   (targeted canary subset only, ...)"`, and pod-with-ReplicaSet-hash
   names). `_live_args()` (`sre_agent/executor.py`) now extracts the
   leading valid DNS-1123 label via regex instead of splitting on `:`,
   which subsumes the old colon-only fix. **Validated live**: confirmed
   `restart_deployment → applied` and, independently, a real new
   ReplicaSet rolled out on `checkout-service` at the k8s level.
4. **`classify_live_response` payload-parsing fix** — `_structured_payload()`
   didn't recurse into the MCP SDK's actual content-block shape
   (`{"type":"text","text":"<json>"}` — JSON under a `text` *key*, not a
   `.text` *attribute*), so genuine tool successes **and** refusals were
   both misreported as `ERROR`. This silently affected every live
   execution, not just restarts (retroactively explains why
   `patch_resource_limits` always showed `error` instead of `refused`).
   Fixed by recursing into the `"text"` key too. Also fixed: the WARNING
   log line was discarding the `detail` string that would have surfaced
   this immediately — now logged. Both fixes covered by unit tests using
   the exact captured real MCP response payloads
   (`tests/test_executor.py`); full suite 780 passed / 2 skipped, 0
   regressions.
5. **Cost optimization, round 1 (model-tier + caching)** —
   `MODEL_ROUTER_FAST_MODEL=claude-haiku-4-5-20251001` in codespace `.env`
   routes ROUTING/NARRATION/GREETING to Haiku 4.5 instead of Sonnet 5;
   SPECIALIST/AGGREGATION/REFLECTION/PLANNING (quality-critical) untouched.
   Hand-rolled Anthropic prompt caching in `sre_agent/model_router.py`
   (`cache_control_marker()`, `cached_system_message()`, `cached_tools()`
   — `langchain_anthropic`'s official middleware only supports
   `langchain.agents.create_agent`, not the `create_react_agent` this
   codebase uses). Applied to specialist system prompts + tool catalogs
   (`agent_nodes.py`), Reflector/Planner system messages
   (`graph_builder.py`), and the supervisor's investigation-planning
   prompt split into a cached static prefix + uncached dynamic suffix
   (`supervisor.py`). Deliberately skipped supervisor's aggregation call
   (no stable static prefix). Config: `ANTHROPIC_PROMPT_CACHE_ENABLED`
   (default true), `ANTHROPIC_PROMPT_CACHE_TTL` (default `1h`).
6. **Cost optimization, round 2 (payload size)** — every MCP tool response
   (7 servers: k8s, prometheus, loki, github, runbooks, github-exec,
   executor) and 5 sre_agent prompt-builder sites (`output_formatter.py`,
   `narrative.py`, `supervisor.py`) were serializing JSON with `indent=2`
   before it entered Claude's context — pure whitespace, zero information
   value. Replaced with compact `separators=(",", ":")` everywhere (79 +
   5 call sites). Same data, smaller payload, no quality impact.

All of #4/#5: tested (774 passed/2 skipped locally), deployed via
SSH-stdin streaming + checksum verification (not `gh codespace cp`,
unreliable), `sre-agent-api` + all 7 `mcp-*` containers rebuilt and
healthy. `sre-agent-api` lives in `platform/docker-compose.yaml`; MCP
servers in `edge_mcp_servers/docker-compose.yaml`.

**Known unrelated issue surfaced during rebuild**: `sre-langfuse-web` /
`sre-langfuse-worker` are crash-looping (ClickHouse `ON CLUSTER default`
migration error — no Zookeeper config in this cluster). Pre-existing
environment issue, not caused by this session's changes, doesn't affect
`sre-agent-api` or the incident pipeline (Langfuse is optional LLM
tracing only) — flagged, not fixed, out of scope.

## Current architecture and invariants
See `docs/ai/DECISIONS.md`. Two independent ACT-phase gates
(`PolicyEngine.evaluate_action()` / `policy_gate.decide()`), plus
`EXECUTOR_LIVE` env var gating `execute_autonomous_live()` for real kubectl
mutations post-approval. `EXECUTOR_TOOL_MAP` (`sre_agent/executor.py:38-44`)
routes `action_type` → live MCP tool name; `config_change` and `patch` BOTH
map to `patch_resource_limits` (semantic mismatch for non-resource-limit
changes — live tool correctly refuses rather than fabricating params; not
yet revisited).

## Active problem
None. Task #16 validated live and `.env` safety flags reverted (see below).
Awaiting user decision on git commit (see Next bounded task).

**Post-revert verification (this session)**: `/workspaces/Sre-automation/.env`
restored to `EXECUTOR_LIVE="false"` (code default per `run_manifest.py`/
`graph_builder.py`) and `SENTINEL_CLUSTER_ENVIRONMENT=production` (documented
default per `.env.example`; also `execution_context.py`'s fail-closed
normalization target for any unset/unrecognized value). `sre-agent-api`
recreated, reached `healthy`, zero errors in logs, `docker exec ... env`
confirms both values loaded correctly. Read `policy_engine.py`'s rule set
first to confirm this restores intended PROD guardrails (blocks `restart`
when `risk_score >= 3.0` — every observed action this session logged `risk:
5.0`, so restarts are now fully blocked in PROD; also blocks `delete`,
scale-to-zero, and `rollback` without explicit approval) without breaking
the investigate/plan/approve pipeline — only the ACT phase's live-mutation
permissiveness is affected, which is the intended, correct behavior.
A DB read against incident `15566b09` right after recreation transiently
showed `status=verification_unknown` — checked `docker logs`, confirmed
benign: the reconciler's next pass immediately flipped it to `resolved`
("Reconciled resolved alert 'CheckoutHighErrorRate' → incident 15566b09
(verification_unknown → resolved)"). Normal transient reconciliation state,
not caused by the revert.

**Dashboard access**: `dashboard/` (Next.js, `platform/docker-compose.yaml`
service `dashboard`, port 3002) is now running on the codespace — start
with `docker compose up -d dashboard` in `platform/` if stopped. Seeded
demo credentials (`admin@example.com`/`admin`) do NOT exist (seeding never
ran against a non-empty DB); the real login is `jayanth.kalyanam@sjsu.edu`
(protected — never reset/touch this account's password) or the synthetic
`sentinel-test-approver@example.com` fixture account (both role=admin, org
`0a687543-4721-46b4-a82c-80479279ebad`, the only org in this deployment).
Approval requests expire ~30 min — an expired one returns HTTP 410 on
click and flips `approval_requests.status` to `expired`; the incident
itself stays `awaiting_approval` and needs the resolve→refire recipe below
for a fresh window.

## Relevant files
- `sre_agent/act_phase.py`, `sre_agent/executor.py` — restart-target +
  loop-abort + classify_live_response fixes.
- `sre_agent/model_router.py`, `agent_nodes.py`, `graph_builder.py`,
  `supervisor.py`, `output_formatter.py`, `narrative.py`,
  `tests/test_model_router.py` — cost-optimization work (rounds 1 + 2).
- `edge_mcp_servers/mcp_servers/{k8s_real,prometheus_real,loki_real,
  github_real,runbooks_notion,github_exec,executor_real}/server.py` —
  compact-JSON payload fix (round 2).
- `edge_mcp_servers/docker-compose.yaml` — MCP port-binding fix.
- All of the above: applied and deployed, **not yet committed to git**.
- `/workspaces/Sre-automation/.env` (codespace) — reverted to
  `EXECUTOR_LIVE="false"`, `SENTINEL_CLUSTER_ENVIRONMENT=production`
  (safe/default values). Live testing requires flipping these again.

## Known blockers or risks
- Approval requests expire in ~30 min — check for lapsed approvals before
  assuming one is still actionable after a gap.
- GitHub Codespaces free tier is capped on core-hours.
- `sre-langfuse-web`/`worker` crash-looping (see above) — cosmetic only.

## Next bounded task
Task #16 is done, `.env` safety flags reverted and verified. Remaining:
decide with user whether/how to commit the accumulated fixes to git (MCP
networking, loop-abort, restart-target parsing, classify_live_response,
cost optimization rounds 1+2 — none committed yet, all deployed only to
the codespace; see `docs/ai/DECISIONS.md` for the two Task #16 root-cause
fixes as durable technical decisions). After that: per
[[decision-production-grade-upgrade]], the production-grade upgrade pass
across the 7 tracked concepts is the natural next milestone — confirm with
user before starting.

## Resolve→refire recipe (for re-testing checkout-service fault)
1. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0`
2. Poll `kubectl exec -n meridian deploy/prometheus -- wget -qO-
   http://localhost:9090/api/v1/alerts` until no `alertname` (~2-5 min,
   5-min rolling window).
3. Confirm incident status flips to `resolved` in Postgres.
4. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0.6`
   to fire a genuinely new incident (dedup matches by title on
   non-resolved incidents only). Planner's proposed actions are
   non-deterministic across identical fault runs — may need multiple
   cycles to get a specific action type in the plan.
