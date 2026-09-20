# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, cost-conscious, and
production-operable. Deterministic policy and durable state—not model prose—
control writes, approvals, status transitions, and operator-facing claims.

## Current milestone
The four-phase context/runbook pass and follow-up specialist hardening are
deployed to Codespace `cuddly-winner-659v67gv695hrxjw` (2026-09-19):

- Four Meridian runbooks published to Notion; a post-write dump scores
  22/22. Backups: `~/Downloads/notion-runbook-backups/`.
- Specialists get the full procedure, exact alert labels/time and bounded
  prior findings; tool results cap at 20k chars, histories at 60k tokens,
  model-directed reads are fail-closed. Detail: `HANDOFF_CODEX.md`
  → "Phase C — implemented result".

All of it is on **`master`** (`2ba66f4`) via PR #55 — Codex's 24 commits,
the Codespace's 20, #53/#54 and the routing fix, merged 2026-09-20 with all
18 CI checks green. Every other branch was fully contained in `master` and
has been deleted; `master` is now the only branch on the remote. The running
stack matches it: `code_sha=2ba66f4…`, parity passed.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant, idempotency, and audit boundary. Approval cannot override a
  hard block.
- Successful actions have durable idempotency records and are not replayed
  after process death. Task #40 remains enforced: a cleared/resolved incident
  cannot restart remediation.
- A task-local investigation marker activates strict read gates only for
  specialist ReAct calls. Deterministic verification keeps tenant isolation
  but bypasses model-search breadth limits.
- Scope refusals are audited as `REFUSED` and returned to the model without
  cancelling sibling calls.

## Completed or verified work
- #48 unwraps ToolCall envelopes before argument enforcement; live smoke went
  from 16/142 successful tools to 53/59. #49 adds `or vector(0)` to counter
  probes and #50 admits only that clause through the PromQL validator; v2
  split digests were regenerated. Notion pagination returns all four runbooks.
- Anthropic prompt-cache TTL is live at 5m, not the costlier 1h override.
- Current smoke `b13ce2c5`: $2.2079, 69 model calls, stopped correctly at human
  approval; this is a floor for a resolving trial.
- Model accounting no longer claims a fallback on every call. `ls_provider`
  names the *integration* (`litellm`), not the route; `_effective_route`
  resolves it from the router's qualified model id. Replaying the 163 real
  records: 163 false claims → 0, with genuine fallbacks still caught.
- CI set `LLM_PROVIDER=anthropic` but never synced the `anthropic` extra, so
  7 tests failed on every run, `master` included. Now syncs it.
- The dashboard has the `restart: unless-stopped` the rest of the stack had.

## Active problem
Autonomy is gated by calibration, not a runtime bug. Uncalibrated confidence
escalates SEV2 to SEV1, which requires approval and makes trials unresolved.
Stage 1 needs at least 40 `live_benchmark` diagnosis observations (normally at
least two trials per 22 scenarios) with `STATISTICAL_RECORDING`; then wire the
diagnosis artifact and config fingerprint. Resolving trials can subsequently
produce remediation calibration.

#53 and #54 made a gated trial carry its full structured grade (smoke2: 4 of
8 scored criteria to 6, `severity_accuracy` `None` to 1.0, remediation FAIL to
PASS), which unblocked #28 without seeding remediation calibration.

Neither answers Codex's objection. The trial schema is closed,
`structured_grade` is deliberately not in it, and `quality_success` still
requires recovery + PASS + safety, so quality and recovery remain zero in every
arm until `statistical_eval`/`ablation_eval` change. At $2.21/incident, one,
two and three trials per scenario are upper-bounded near $194, $389 and $583.

Calibration support is the cheaper path and needs no autonomy: escalation is
gated on the **diagnosis** artifact, and a gated trial already emits a
diagnosis observation (smoke2: `diagnosis_confidence=0.86`, outcome `False`).
At `minimum_threshold_support=40` that is ~40 gated incidents, ≈**$88**. Two
caveats: `STATISTICAL_RECORDING` has never run on this stack, and it is
unverified whether a below-support artifact could flip
`hypothesis_confidence_calibrated` true by returning non-None with a null
threshold.

The local runbook corpus snapshot is pre-publish and stale; regenerate it
before using it to describe Notion.

## Relevant files
- Query/context: `sre_agent/{namespace_scope,mcp_tool_wrapper,agent_nodes,
  context_compaction,narrative,audit_context}.py`, `config/agent_config.yaml`.
- GitHub payload: `edge_mcp_servers/mcp_servers/github_real/{payload,server}.py`.
- Benchmark: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench}.py`,
  `benchmarks/datasets/v2/`.
- Operations and rationale: `docs/ai/HANDOFF_CODEX.md`, `DECISIONS.md`.

## Verification commands and latest results
- CI on `master` (run 35494662328, 2026-09-20), all 18 checks green:
  **1,966 passed** + 19 integration, 41 warnings, 65% coverage. This is the
  first green run — the suite had been red since before the reconciliation.
- `scripts/audit_runbook_coverage.py` → 22/22 against the fresh live dump.
- `scripts/audit_runbook_controls.py` → 4 detected, 1 known blind.
- `check_python_quality.sh`, `check_eval_smoke.sh`, secret scan → pass.

## Known blockers or risks
- `remediation_gate_approvals` has a single writer
  (`sre_agent/approval_flow.py:460`) that has never fired, against 61
  `approval_requests` (44 expired, 17 approved). Probably because autonomous
  remediation has never run; unconfirmed.
- `_scope_query` injects tenant namespace into only the first selector block;
  multi-selector PromQL remains incompletely scoped.
- Grades are not comparable across the #54 fix: the rubric method was renamed
  to `typed_remediation_action_match`, moving its sha256, so smoke2's recorded
  grade belongs to the old semantics. Nothing paid has run, so nothing else is.
- `/app/reports` is not a volume; copy accounting/traces out before rebuilds.
- Never stage `.agents/` or `.env.local-backup-20260910`; never `git add -A`.

## Next bounded task
The user set the order: every backend component working as intended, then
the frontend wired, then benchmarking.

**(0) and (1) are done** — deployed at `code_sha=2ba66f4…` with parity
passing, and CI runs the suite green, so use CI as the gate rather than a
local pytest run.

**(2) has two checks left.** Prove `STATISTICAL_RECORDING`, which has never
run on this stack, persists `cost_usd` and confidence records; and check
whether a below-support calibration artifact can spuriously set
`hypothesis_confidence_calibrated`.

**(3) then the frontend.** Six API modules have no caller (`invitations`,
`jobs`, `mission_control`, `ownership`, `tickets`, `ws_tickets`). The data
layer is axios, not `fetch` — 49 calls across ~25 endpoints, so the UI is far
more wired than a `fetch(` grep suggests.

**(4)** Only then the campaign shape, ~$88 versus the $194+ tier, and not
without the user's stage and budget.

`docs/ai/HANDOFF_CODEX.md` → "The current plan" holds the evidence, the sweep
numbers and the trap for each step.
