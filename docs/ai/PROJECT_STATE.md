# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, cost-conscious, and
production-operable. Deterministic policy and durable state—not model prose—
control writes, approvals, status transitions, and operator-facing claims.

## Current milestone
**A fresh install needs no `.env`.** Everything an operator used to set before
first boot is generated, claimed, or configured in the dashboard. Five commits
on `master` (2026-09-20), on top of PR #55's `2ba66f4`:

- `12ea83d` the three irreducible secrets (`SECRET_KEY`,
  `CREDENTIAL_ENCRYPTION_KEY`, `MCP_SERVICE_TOKEN`) generate into a persisted
  keystore volume on first boot; operator-supplied env still wins.
- `8ca7497` a first-run claim page creates the first admin + org while the
  install has zero users, then closes permanently. No seed account.
- `348aba9` `environment` and `approval_ttl_minutes` move onto the cluster
  row (migration `a7b8c9d0e1f2`); `ACT_PHASE_ENABLED` retired — it gated
  nothing, `_act_phase_enabled()` returned `True` unconditionally.
- `5a1e13d` `.env.example` declared OPTIONAL; the six never-read `SEED_*`
  vars and the LangSmith block deleted. `17efbd8` repins the alembic head.

Per-cluster LLM keys/models, observability endpoints, GitHub/Jira/Notion,
Slack and Langfuse were already Settings-page-configured before this.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant, idempotency, and audit boundary: approval cannot override a
  hard block, successful actions are not replayed after process death, and a
  cleared incident cannot restart remediation (#40).
- Strict read gates activate only for specialist ReAct calls; deterministic
  verification keeps tenant isolation but bypasses breadth limits. Scope
  refusals audit as `REFUSED` without cancelling sibling calls.
- Policy columns are **nullable = inherit**: null means "no per-cluster
  opinion, use the deployment default", so the migration backfills nothing.
  Resolution fails closed to `production` / 30 minutes; the schema `Literal`
  turns a typo into a visible 422 rather than a silently normalised setting.
- Losing the keystore volume makes every stored credential unrecoverable —
  the accepted cost of zero-config boot. Back it up.

## Completed or verified work
- #48/#49/#50: ToolCall envelopes unwrapped before argument enforcement (live
  smoke 16/142 → 53/59 successful tools); `or vector(0)` admitted through the
  PromQL validator. Four Meridian runbooks live in Notion, scoring 22/22.
- Model accounting: replaying the 163 real records turns 163 false fallback
  claims into 0, genuine fallbacks still caught. Cache TTL is 5m, not 1h.
- Smoke `b13ce2c5`: $2.2079, 69 model calls, stopped correctly at human
  approval — a floor for a resolving trial.

## Active problem
Autonomy is gated by calibration, not a runtime bug. Uncalibrated confidence
escalates SEV2 to SEV1, which requires approval and leaves trials unresolved.
Stage 1 needs at least 40 `live_benchmark` diagnosis observations with
`STATISTICAL_RECORDING`, then the diagnosis artifact and config fingerprint.

#53/#54 gave a gated trial its full structured grade but do not answer Codex's
objection: the trial schema is closed, `quality_success` still requires
recovery + PASS + safety, so quality and recovery stay zero in every arm until
`statistical_eval` / `ablation_eval` change. At $2.21/incident, one, two and
three trials per scenario bound near $194, $389 and $583. Calibration support
is cheaper and needs no autonomy: escalation gates on the *diagnosis*
artifact, which a gated trial already emits, so ~40 incidents, ≈**$88**.

## Relevant files
- Zero-env: `sre_agent/bootstrap_secrets.py`, `platform/start.sh`,
  `backend/alembic/versions/a7b8c9d0e1f2_*.py`,
  `sre_agent/{execution_context,approval_flow}.py`,
  `dashboard/app/(dashboard)/clusters/[id]/settings/page.tsx`.
- Query/context: `sre_agent/{namespace_scope,mcp_tool_wrapper,agent_nodes,
  context_compaction,narrative,audit_context}.py`, `config/agent_config.yaml`.
- Benchmark: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench}.py`,
  `benchmarks/datasets/v2/`.
- Operations and rationale: `docs/ai/HANDOFF_CODEX.md`, `DECISIONS.md`.

## Verification commands and latest results
- CI on `master`, run 35525691433 (2026-09-20): all 18 checks green,
  **2,009 passed** + 19 integration, 41 warnings.
- Migration `a7b8c9d0e1f2` applied from empty, downgraded and re-upgraded
  against a throwaway DB. Live `sre_platform` is still at the old head.
- `scripts/audit_runbook_coverage.py` → 22/22 against the live dump;
  `audit_runbook_controls.py` → 4 detected, 1 known blind.
- `check_python_quality.sh`, `check_eval_smoke.sh`, secret scan → pass.
- The new migration is **not** applied to the running `sre_platform` DB.

## Known blockers or risks
- `STATISTICAL_RECORDING` has never run on this stack, and it is unverified
  whether a below-support calibration artifact can spuriously set
  `hypothesis_confidence_calibrated` by returning non-None with a null
  threshold.
- `remediation_gate_approvals` has a single writer (`approval_flow.py:460`)
  that has never fired, against 61 `approval_requests`. Probably because
  autonomous remediation has never run; unconfirmed.
- `_scope_query` injects tenant namespace into only the first selector block.
- Grades are not comparable across the #54 fix: the rubric method rename moved
  its sha256. Nothing paid has run, so nothing else is affected.
- `/app/reports` is not a volume; copy accounting/traces out before rebuilds.
- The local runbook corpus snapshot is pre-publish and stale.
- Never stage `.agents/` or `.env.local-backup-20260910`; never `git add -A`.

## Next bounded task
The user's order: backend verified, then the frontend wired, then
benchmarking. (0), (1) and the zero-env work are done.

**(2) has two checks left** — the two `STATISTICAL_RECORDING` items above.

**(3) then the frontend.** Six API modules have no caller (`invitations`,
`jobs`, `mission_control`, `ownership`, `tickets`, `ws_tickets`). The data
layer is axios, not `fetch` — 49 calls across ~25 endpoints, so the UI is far
more wired than a `fetch(` grep suggests.

**(4)** Only then the campaign shape, ~$88 versus the $194+ tier, and not
without the user's stage and budget.

`docs/ai/HANDOFF_CODEX.md` → "The current plan" holds the evidence and traps.
