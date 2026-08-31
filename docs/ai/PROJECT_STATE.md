# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
The 5-phase upgrade plan is complete and merged (PRs #44-#49), plus
post-plan work: runbooks migrated to Notion-only hosting (`f75196d`),
dashboard UI brought to parity with the backend — Notion/Jira/Slack/GitHub
App surfaces (`a0e0ff6`, `0d48f11`), and a README quickstart (`90d81ce`).
`master` HEAD is `90d81ce`.

Current focus has shifted from feature work to **operational hardening via
live testing**: firing real alerts through the full pipeline (not unit
tests) and fixing what breaks. This session found and fixed 6 bugs this way
(see below), verified the Slack integration end-to-end against the real
Slack API, stood up a GitHub Codespace as a faster alternative to running
the stack locally, and verified the approve/remediate API mechanics on the
codespace. The local Docker stack has now been stopped by the user; the
codespace (`jubilant-space-invention-4vjq497q4x63jx5q`) is the only running
environment.

## Current architecture and invariants
See `docs/ai/DECISIONS.md` for the full rationale behind each of these.
Unchanged from prior sessions: strict LLM provider guard
(`sre_agent.provider_config.SUPPORTED_PROVIDERS`), single canonical runner
(`sre_agent.incident_runner.run_incident_investigation`), durable
Postgres-lease job worker (`sre_agent/job_worker.py`), mutation/sandbox
gateways, per-cluster Jira/Notion/relay credential patterns, Qdrant-backed
tenant-scoped incident memory, Alembic single head (`a3f7c1d9b2e4`).

New this session:
- **MCP servers must cap output size for every query shape, not just
  range queries.** Prometheus's `get_metric` (instant) and
  `get_golden_signals` now go through `_cap_vector_result()`; Loki's
  `_cap_logs()` now handles the zero-result case correctly. See
  DECISIONS.md.
- **Anthropic extended-thinking responses return `AIMessage.content` as a
  list of blocks, not a string.** Any code touching `.content` off an
  `AIMessage`/LLM response must guard for this (`isinstance(content, list)`,
  extract `type == "text"` blocks) before doing string ops. Fixed at the two
  real call sites: `agent_nodes.py` (~line 296) and `supervisor.py` (~line
  1490). `narrative.py::_invoke_llm()` already had this guard.
- **Two separate `.env` files exist and are easy to confuse:**
  `platform/.env` is compose-time variable substitution only;
  the repo-root `.env` holds the real secrets containers read via
  `env_file:`. Editing the wrong one silently breaks or fails-open.
- **Cloud dev environments are synced via `rsync`, never `git push`** —
  see DECISIONS.md. A remote box's Postgres/Redis start empty; local
  incident data does not carry over automatically (this session's 6
  incidents were later migrated to the codespace via `pg_dump`/`pg_restore`).
- **The `meridian` cluster's synthetic telemetry source is outside this
  repo, bound to the local laptop's Docker host.** `clusters.prometheus_url`
  / `loki_url` are `http://host.docker.internal:9090` / `:3100` — this
  resolves only under local Docker Desktop, never from a remote box like a
  Codespace. No demo/synthetic Prometheus+Loki stack exists anywhere in
  this repo (checked `platform/` and `edge_mcp_servers/` compose files).
  Any environment without access to whatever serves those two ports
  locally will see every investigation come back with no real metrics/log
  evidence, `severity: UNKNOWN`, and the agent correctly refusing to act.
- **The approve endpoint gates in two independent layers, not one.** The
  human-facing `POST /api/v1/incidents/{id}/approve` only resolves the
  incident-level `ApprovalRequest` interrupt (hash + expiry checked against
  the `approval_requests` table, TTL = `APPROVAL_TTL_MINUTES`, default 30).
  Resuming the graph then runs `act_phase.py`'s *per-action* policy-gate
  autonomy check independently; an action only actually (dry-run) executes
  if that check returns `AUTONOMOUS`, not just because the outer approval
  succeeded. Low-confidence/no-evidence investigations get every action
  marked `REQUIRES_APPROVAL`/`BLOCKED` here regardless, so the incident
  ends at `investigated`, not `resolved`, with 0 actions executed — by
  design, not a bug.

## Completed or verified work
- All prior phases (1-5) + runbooks-to-Notion + dashboard parity: merged,
  see git log.
- **Slack integration — verified end-to-end against the live Slack API**
  (not just log-absence-of-error): bot posted incident-open + streamed an
  8-message timeline into thread `C0BUTRSPY3A` in `#all-sentinel`, confirmed
  via `conversations.replies`.
- **6 bugs found via live alert traffic and fixed, this session:**
  1. Loki `query_logs` context-overflow cap (fixed a 2.4M-token blowup from
     crash-loop logs).
  2. Loki `_cap_logs` crash on empty results (`**None` from a falsy-empty-list
     `while`).
  3. Prometheus `get_metric`/`get_golden_signals` had no output cap (likely
     source of a "1.1M tokens > 1M max" overflow).
  4. **Uniform crash across all 4 specialist agents** from extended-thinking
     list-typed `.content` — fixed in `agent_nodes.py` + `supervisor.py`.
     Verified via regression alert `DiskPressureCritical2` → incident
     `2bd886da-b1b7-4884-ac18-5c069f35a949`: clean single-attempt run,
     zero `'list' object has no attribute 'strip'` occurrences in the full
     timeline or container logs (prior attempt `8a10508b` hit this exact
     error on all 3 investigating specialists before the fix).
  5. False-positive numeric-fact conflict detection in
     `incident_timeline.py` (threshold-vs-observed disambiguation).
  6. Empty "Objective: the incident" fallback in
     `graph_builder.py::_prepare_initial_state`.
- `platform/.env` operational incident: wrong-file edit broke compose-time
  substitution; root-caused, repo-root `.env` secrets confirmed untouched
  throughout, `platform/.env` restored.
- Stood up GitHub Codespace `jubilant-space-invention-4vjq497q4x63jx5q`
  (4-core/16GB, Docker preinstalled) as a faster test environment; local
  repo synced via `rsync`; full stack including dashboard now running
  there (`sre-agent-api` + `sre-dashboard` both healthy on ports 8080/3002).
  All 6 open incidents from local were migrated in via `pg_dump`/`pg_restore`.
- **Approve/remediate API mechanics — verified working on the codespace,
  twice.** Created a throwaway admin account
  (`sentinel-test-approver@example.com`) via the real `/auth/register` flow,
  reassigned into the `meridian` org via one manual SQL `UPDATE` (run by the
  user directly — mutating `users` is blocked for the agent by the auto-mode
  classifier). Fired two fresh synthetic alerts, drove each to
  `awaiting_approval`, and called `POST /api/v1/incidents/{id}/approve` (run
  by the user directly — the classifier blocks this mutating call from the
  agent too) with the correct `approval_request_id`/`action_hash`. Both
  calls returned `{"status":"RESUMED", ..., "completed":true}` and the
  underlying job reached `status=completed` — confirms the endpoint's
  hash/expiry validation and LangGraph `Command(resume=...)` resumption are
  correct. Neither incident reached a real autonomous-execution+resolve
  outcome, for the telemetry-unreachability reason above, not an API bug.
  Incident `2bd886da`'s original approval expired mid-session (30 min TTL)
  during the auth/account-setup detour; testing continued on fresh
  incidents `9aeba020` and `78969d6d` instead.

## Active problem
The approve→remediate→verify/resolve pipeline's **API mechanics** are now
verified (see above). What's still unverified: a real **autonomous-execution
+ resolve** outcome — every attempt so far has ended at `investigated` with
0 actions executed, because no environment (codespace or, now, local) has
reachable Prometheus/Loki telemetry for the `meridian` cluster. 8 incidents
now sit unresolved on the codespace: the original 6 from local (`d8bf6fbe`,
`f89eb01f`, `f7a0b04d`, `6c1deea8`, `8a10508b`, `2bd886da` — `2bd886da`'s
approval has since expired) plus this session's two fresh test incidents
(`9aeba020`, `78969d6d`, both ended `investigated`, both held-for-approval
with 0 actions executed).

The user has stopped the local Docker stack. The codespace is now the only
running environment; there is currently no reachable source for the
`meridian` cluster's synthetic Prometheus/Loki data anywhere.

`langfuse-web`/`langfuse-worker` crash-loop on a ClickHouse
`ReplicatedMergeTree`/Zookeeper migration error, both locally (when it was
running) and on the codespace — pre-existing, unrelated to this session's
fixes, non-blocking (`LANGFUSE_TRACING=false`).

## Relevant files
- `edge_mcp_servers/mcp_servers/{prometheus_real,loki_real}/server.py` —
  capping fixes (uncommitted)
- `sre_agent/agent_nodes.py` (~line 296-318), `sre_agent/supervisor.py`
  (~line 1490) — extended-thinking content-list fix (uncommitted)
- `sre_agent/incident_timeline.py`, `sre_agent/graph_builder.py` — bugs 5/6
  (uncommitted)
- `pyproject.toml`, `uv.lock` — `slack-bolt` dependency added (uncommitted)
- `platform/docker-compose.yaml`, `platform/.env`, repo-root `.env` — env
  split incident (uncommitted)
- dashboard `page.tsx` files — uncommitted, predates this session's bug-fix
  work, not yet investigated in this session

## Verification commands and latest results
- Regression-test pattern used throughout: fire an alert via
  `/Users/jayan/.claude/jobs/3f02b96f/tmp/fire_alert.py` with a **distinct**
  `alertname` (same-name re-fires get deduped into the existing open
  incident, not a fresh run) → poll
  `SELECT status FROM incidents WHERE id=...` /
  `SELECT status, attempt_count FROM jobs WHERE incident_id=... ORDER BY
  created_at DESC LIMIT 1` until terminal → pull
  `incident_timeline_events` (`event_type || '|' || title || '|' ||
  content`) into a file and grep for the specific error text, plus
  `docker logs sre-agent-api --since <window> | grep -i
  "attributeerror\|has no attribute\|Error in .* Agent"`.
- Latest run (`2bd886da`): `awaiting_approval`, `attempt_count=1`, 0 matches
  in both the timeline grep and the container-log grep — the fix is
  confirmed, not just assumed from silence.
- No `pytest`/`ruff`/release-gate run this session (the work was
  live-integration bug-fixing against a running stack, not a code-review
  pass) — worth running before any of this gets committed for real.

## Known blockers or risks
- Real end-to-end OAuth validation (GitHub App + Slack app) still deferred
  to the user, per prior sessions.
- Phase 5's AIOpsLab adapter still never run against the real package/live
  cluster.
- GitHub Codespaces free tier is capped on monthly core-hours and not meant
  to run persistently — remember to `gh codespace stop` when done with it.
- **No reachable synthetic-telemetry source for the `meridian` cluster
  right now** (local stopped; codespace can't reach `host.docker.internal`)
  — blocks getting a real autonomous-execution+resolve test result until
  either the local stack (and whatever serves `:9090`/`:3100` on it) is
  restarted, or the cluster's `prometheus_url`/`loki_url` are repointed at
  something reachable from wherever testing happens next.
- The `users` table and the `/approve` endpoint are both blocked for
  direct agent Bash calls by the auto-mode classifier (mutations on
  auth-sensitive surfaces) — any future approve-flow testing needs the user
  to run those specific commands, as was done this session.
- Commit `1d8ea60` (this session's 6 bug fixes + dependency/env changes) was
  never run through `uv run pytest -q` / `ruff check` before committing —
  still worth doing.

## Next bounded task
Stand up the Meridian demo app **inside the codespace** so telemetry no
longer depends on the laptop at all:
1. The user's local `host.docker.internal:9090`/`:3100` telemetry came from
   a Kubernetes cluster run via **OrbStack** (macOS-only, can't run in a
   Codespace) hosting the **Meridian demo app**:
   https://github.com/jayanth922/meridian-shop (public repo — "production-
   standard e-commerce demo/target app: services, monitoring stack, load
   generator, MCP signals server" — likely ships its own Prometheus/Loki
   and self-generates traffic, so no separate data-faking needed).
2. Plan: install `kind` in the codespace (pure Linux, works over Codespaces'
   existing Docker — no OrbStack dependency), deploy `meridian-shop` into
   it, apply this repo's `deploy/examples/meridian` overlay to point
   Sentinel at it, then update the `clusters` row's `prometheus_url`/
   `loki_url` from `host.docker.internal:*` to the in-cluster/NodePort
   addresses.
3. Not yet started — deploy method used on OrbStack (`kubectl apply` vs.
   Helm vs. script) not yet confirmed; check `meridian-shop`'s own
   README/manifests first.

Fallback, if the above stalls: `uv run pytest -q` and `ruff check` on the 6
bug-fix files from commit `1d8ea60` — deferred all session, needs no live
stack.
