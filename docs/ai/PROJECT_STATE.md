# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 — deterministic remediation pipeline (Temporal-orchestrated, two
manual approval gates per issue, per-issue isolated chat/PR, concurrent-
incident correlation) — **complete and live-fire validated end-to-end as of
2026-09-03**, including a real GitHub PR write against
`jayanth922/meridian-shop` (test-18, PR #1, closed) and the bounded
retry-loop/close-incident flow (test-11). Full design in
`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`; implementation history and
every bug found/fixed along the way is in git log and
`docs/ai/DECISIONS.md` — not restated here.

Also done since: Hermes actor backend fully removed (single backend now,
`LocalTerminalRuntime` — see DECISIONS.md "Hermes removal"), execution-trace
dashboard timeline, and a UI/UX pass (error states, loading states, dead CSS,
accessibility labels — responsive/mobile layout deliberately deferred).

Also done, 2026-09-04 (resource-optimization pass, unrelated to Phase 5
logic): Temporal (`sre_agent/temporal_client.py`) now supports both a local
dev-server (`temporalio/temporal:latest server start-dev`, opt-in
`COMPOSE_PROFILES=local-temporal`) and Temporal Cloud
(`TEMPORAL_HOST`/`TEMPORAL_API_KEY`, TLS auto-enabled with an API key);
self-hosted Langfuse (clickhouse/minio/langfuse-web/-worker) deleted
entirely from `docker-compose.yaml`, `.env.example`, and the Helm chart —
Langfuse Cloud (free tier) is now the only tracing backend. Commit
`4804da5`, pushed to `origin/master`. Same day: `sre_agent/code_sandbox.py`
(an opt-in E2B microVM backend added earlier the same day) removed entirely
— zero production callers, pure duplication of `sandbox_workflow.py`'s
K8s-Job-based sandbox, which is the sole code-fix sandbox mechanism now. See
`docs/ai/DECISIONS.md` "E2B sandbox backend removed".

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` env var gating
`execute_autonomous_live()`. `EXECUTOR_TOOL_MAP` (`executor.py:38-44`) routes
`action_type` → live MCP tool name. See `docs/ai/DECISIONS.md` for the full
rationale log (Task #16 root causes, cloud-dev-env-via-rsync convention).

## Completed or verified work
Pre-Phase-5: Task #16 live-fire validation; model tiering/prompt caching +
cross-provider routing; Temporal code-fix verification; runbook RAG/NL-query
(PR #53); ad hoc Slack chat memory (PR #54). Phase 5 A–F + cutover, retry
loop, real PR-write path, execution-trace view, Hermes removal, UI/UX pass:
all done, live-fire validated, merged to `origin/master` — see git log for
commit-by-commit detail.

## Active problem
Phase 5 is done. Two items remain, deferred until Phase 5 completed per
standing instruction (`decision-production-grade-upgrade` memory): the
**responsive/mobile layout pass** (not started) and the **AIOpsLab domain
benchmark** (not started). The prior session's planned full manual
end-to-end frontend test (account creation → cluster connect → incident →
resolved/closed) — status unconfirmed as of this session; this session's
work was infra/observability-stack cleanup only and didn't touch or verify
that flow. Confirm whether it ran before assuming it did.

## Relevant files
- `sre_agent/incident_remediation_workflow.py` — the two-gate Temporal
  workflow, retry loop, close-incident handoff.
- `sre_agent/actor_runtime.py` — deterministic actor (`LocalTerminalRuntime`),
  sole backend.
- `sre_agent/graph_builder.py::_act_gate_node` — deterministic-pipeline
  detection/deferral.
- `sre_agent/service_topology.py`, `sre_agent/incident_correlation.py` —
  correlation-gate adjacency (Phase A).
- `edge_mcp_servers/mcp_servers/sandbox_real/` — sandbox-verify MCP server.
- `edge_mcp_servers/mcp_servers/github_exec/server.py` — `create_fix_pr`/
  `create_revert_pr` (real PR-write path).
- `sre_agent/approval_flow.py`, `sre_agent/api/v1/remediation_gates.py`,
  `backend/models.py::RemediationGateApproval` — gate persistence/API.
- `dashboard/app/(dashboard)/clusters/[id]/incidents/[incidentId]/page.tsx`
  — gate approval panel + execution-trace timeline.
- `sre_agent/war_room.py`, `sre_agent/integrations/slack_bot.py` — Slack
  gate-decision commands.
- `docs/ai/DECISIONS.md` — durable technical decisions; check before
  re-deriving root causes already documented there.
- `sre_agent/temporal_client.py` — Temporal bootstrap (local dev-server vs
  Cloud, `TEMPORAL_ENABLED`/`TEMPORAL_API_KEY`).

## Verification commands and latest results
Full suite green as of commit `4804da5` (889 passed, 3 skipped); `helm lint`
and `helm template` clean on `deploy/helm/sentinel` after the Langfuse
template deletion; `docker compose --env-file .env.example -f
platform/docker-compose.yaml config` clean (no Langfuse services, `temporal`
correctly inactive by default). Re-run: `pytest`, `ruff check .`, `mypy .` —
see `docs/ai/DECISIONS.md`/git log if a specific historical count is needed.

## Known blockers or risks
- GitHub Codespaces free tier is capped on core-hours — stop
  `jubilant-space-invention-4vjq497q4x63jx5q` when idle (`gh codespace stop`).
  Platform stack is fully torn down (no containers/volumes) and the
  Codespace itself stopped as of 2026-09-03, pending the end-to-end frontend
  test run.
- Approval requests (`ApprovalRequest` and `RemediationGateApproval`) expire
  ~30 min (`APPROVAL_TTL_MINUTES`) — see resolve→refire recipe below if
  re-testing live execution during that run.

## Next bounded task
Confirm whether the pending manual end-to-end frontend test (account
creation → cluster settings → incident detected → remediation →
resolved/closed) ran; if not, run it and watch logs live. Otherwise, pick up
either the responsive/mobile layout pass or the AIOpsLab domain benchmark
(both deferred, neither started).

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
