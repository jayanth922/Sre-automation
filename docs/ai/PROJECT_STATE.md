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
Slack API, and stood up a GitHub Codespace as a faster alternative to
running the stack locally (RAM-constrained laptop).

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
  incident data does not carry over automatically.

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
  repo (including these uncommitted fixes) synced via `rsync`; full stack
  minus dashboard running there, `sre-agent-api` healthy.

## Active problem
The human-approval remediation pipeline (approve → remediate → verify/
resolve) is **completely untested this session**. 6 incidents currently sit
at `awaiting_approval` with nothing approved, remediated, or resolved:
`d8bf6fbe` (api-gateway DiskPressureHigh — the original bug-triggering
incident), `f89eb01f` (inventory-service PodCrashLooping), `f7a0b04d`
(payment-service HighLatency), `6c1deea8` (checkout-service HighErrorRate),
`8a10508b` (regression test, pre-fix), `2bd886da` (regression test,
post-fix, clean). This data lives only in the local Postgres — the
Codespace's Postgres is a fresh empty volume with none of it.

`langfuse-web` crash-loops on a ClickHouse `ReplicatedMergeTree`/Zookeeper
migration error, both locally and on the fresh Codespace — pre-existing,
unrelated to this session's fixes, non-blocking (`LANGFUSE_TRACING=false`).

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
- 20 files uncommitted (see "Relevant files"); no `pytest`/`ruff` pass has
  been run against them yet this session.

## Next bounded task
Exercise the approve → remediate → verify/resolve flow on one of the 6 open
incidents (recommend `2bd886da`, the cleanest recent one) — this stage of
the pipeline has zero coverage this session. Before or after that, run
`uv run pytest -q` and `ruff check` on the 6 changed source files to catch
anything a live-traffic test wouldn't surface, then commit.
