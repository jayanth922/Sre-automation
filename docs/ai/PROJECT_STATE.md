# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, cost-conscious, and
production-operable. Deterministic policy and durable state—not model prose—
control writes, approvals, status transitions, and operator-facing claims.

## Current milestone
**Backend verification is closed; the frontend is next.** A fresh install
needs no `.env`: the three irreducible secrets generate into a keystore volume
on first boot, a first-run claim page creates the first admin + org and then
closes, and `environment` / `approval_ttl_minutes` moved onto the cluster row
(migration `a7b8c9d0e1f2`). Shipped 2026-09-20 as `12ea83d`, `8ca7497`,
`348aba9`, `5a1e13d`, `17efbd8` over PR #55's `2ba66f4`; `ACT_PHASE_ENABLED`
retired as dead. LLM keys/models, observability, GitHub/Jira/Notion, Slack and
Langfuse were already Settings-configured.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant, idempotency, and audit boundary: approval cannot override a
  hard block, successful actions are not replayed after process death, and a
  cleared incident cannot restart remediation (#40).
- Strict read gates activate only for specialist ReAct calls; deterministic
  verification keeps tenant isolation but bypasses breadth limits. Scope
  refusals audit as `REFUSED` without cancelling sibling calls.
- Policy columns are **nullable = inherit**; resolution fails closed to
  `production` / 30 minutes and a typo is a 422, not a silent normalisation.
- Losing the keystore volume makes every stored credential unrecoverable —
  the accepted cost of zero-config boot. Back it up.

## Completed or verified work
- #48/#49/#50: ToolCall envelopes unwrapped before argument enforcement
  (live smoke 16/142 → 53/59 tools). Runbooks live in Notion, 22/22.
- Smoke `b13ce2c5`: $2.2079, 69 model calls, stopped correctly at human
  approval — a floor for a resolving trial.
- Calibration gate (`2de46bb`): an artifact that blocked its own autonomy
  threshold no longer counts as calibration; severity reads the mapped
  probability, not the self-report.
- Codex's four zero-cost suggestions, uncommitted on `master`: a plan naming
  the same mutation twice now applies it once (`act_phase.py`, deduped
  *after* target canonicalization — that is what creates most collisions); an
  MCP server answering 200 with `{"error": ...}` raises inside the retry loop
  instead of reaching the specialist as a finding (`mcp_tool_wrapper.py`;
  `isError` was never the gap, the adapter already raises on it); a human
  `mark resolved` writes an `incident_resolved` row naming the actor
  (`approval_flow.py`); SLO burn rates are measured, not `None` (`slos.py`).
- Recording path (`de0577d`): a gated trial **does** emit its diagnosis
  observation, so the ~$88 path is reachable, and a malformed
  `BENCH_CONFIG_FINGERPRINT` is refused at import, not after a paid incident.

## Active problem
Autonomy is gated by calibration, not a runtime bug: uncalibrated confidence
escalates SEV2 to SEV1, which needs approval and leaves trials unresolved.
Stage 1 needs >=40 `live_benchmark` diagnosis observations under
`STATISTICAL_RECORDING`, then the artifact and fingerprint.

#53/#54 gave a gated trial its full structured grade, but `quality_success`
still requires recovery + PASS + safety, so quality and recovery stay zero in
every arm until `statistical_eval` / `ablation_eval` change. Everything costs
`arms × scenarios × trials × $2.21`: the paired ablation tiers bound near
$194 / $389 / $583, while calibration needs no autonomy (escalation gates on
the *diagnosis* artifact, which a gated trial already emits) — ~40 incidents,
≈**$88**, unauthorized.

## Relevant files
- Zero-env: `sre_agent/bootstrap_secrets.py`, `platform/start.sh`,
  `dashboard/.../clusters/[id]/settings/page.tsx`.
- Query/context: `sre_agent/{namespace_scope,mcp_tool_wrapper,agent_nodes,
  context_compaction,audit_context}.py`, `config/agent_config.yaml`.
- Benchmark: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench}.py`,
  `benchmarks/datasets/v2/`.
- Operations and rationale: `docs/ai/HANDOFF_CODEX.md`, `DECISIONS.md`.

## Verification commands and latest results
- CI on `master`, run 35530262953 (2026-09-20): all 18 checks green.
- Full suite in the Codespace: **2068 passed, 6 skipped** (`uv sync --frozen
  --extra dev --extra temporal --extra anthropic` — `--extra dev` alone fails
  7 on missing `temporalio` / `langchain_anthropic`).
- No pytest on this host or in `sentinel/api:local`; benchmark tests run in
  a throwaway container via a shim. `test_scenario_dataset` 29,
  `test_statistical_eval` 14, `test_statistical_recording` 9 — all pass.
- Migration `a7b8c9d0e1f2` applied from empty, downgraded and re-upgraded
  against a throwaway DB. Live `sre_platform` is still at the old head.
- `audit_runbook_coverage.py` → 22/22; `audit_runbook_controls.py` → 4
  detected, 1 known blind. Quality, eval-smoke and secret scans pass.

## Known blockers or risks
- `STATISTICAL_RECORDING` is unit-verified but has still never produced a
  row from a live trial on this stack.
- `remediation_gate_approvals` is 0 against 61 `approval_requests` because
  its only writer, `open_gate_activity`, runs only for a **code-fix** action
  (`graph_builder.py:731`). v2 scenarios are infra faults, so that Temporal
  pipeline has never started — a second subsystem, not a broken one.
- Grades are not comparable across the #54 fix: the rubric method rename moved
  its sha256. Nothing paid has run, so nothing else is affected.
- Never stage `.agents/` or `.env.local-backup-20260910`; never `git add -A`.

## Next bounded task
The user's order: backend verified, then frontend, then benchmarking. (0),
(1), (2) and the zero-env work are done — both `STATISTICAL_RECORDING` checks
are closed.

**(3) the frontend is wired.** The three real orphans are done —
`invitations` (403d4e7), `jobs` and `tickets`. Mission control's `/approve`
and `/mark-resolved` are Slack-only by design, not orphans.

**(4)** Benchmarking next: the campaign shape, ~$88 versus the $194+ tier,
needs the user's stage and budget. `ablation_eval.py:319` pins `code_sha`
across arms, so a paid campaign cannot be staged across code changes — land
every pending fix first. The free pre-flight (`benchmarks/ablation_coverage.py`
and the other checks the handoff lists) has not been run.

Codex's three remaining suggestions are not zero-cost: artifact-backed
context / observability semantics / job-completion ownership are $0 in API
spend but a large refactor; #44 and #41 stay unproven until something paid
runs; a missing-data v3 corpus is real new spend.

`docs/ai/HANDOFF_CODEX.md` → "The current plan" holds the evidence and traps.
