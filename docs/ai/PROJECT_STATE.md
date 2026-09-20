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
- Specialists receive the full selected procedure, exact alert labels/time
  and bounded prior findings; tool results cap at 20k chars, histories at 60k
  tokens, and durable evidence stays lossless. Model-directed reads are
  fail-closed on target/window/limit. Detail lives in `HANDOFF_CODEX.md`
  → "Phase C — implemented result".

All of it is merged and pushed as **`merge/codex-reconcile`** (`f45b6e4`):
Codex's 24 commits, the Codespace's 20, and #53/#54. Whether that branch
becomes a PR into `master` is the user's call.

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
- #48 unwraps LangChain ToolCall envelopes before argument enforcement; live
  smoke improved from 16/142 successful tools to 53/59.
- #49 adds `or vector(0)` to counter probes so healthy absent counters produce
  zero; v2 split digests were regenerated. #50 allows only that exact clause
  through the PromQL validator.
- Notion pagination now returns all four runbooks in the live container.
- Anthropic prompt-cache TTL is live at 5m, not the costlier 1h override.
- Current smoke `b13ce2c5`: $2.2079, 69 model calls, stopped correctly at human
  approval; this is a floor for a resolving trial.

## Active problem
Autonomy is gated by calibration, not a runtime bug. Uncalibrated confidence
escalates SEV2 to SEV1, which requires approval and makes trials unresolved.
Stage 1 needs at least 40 `live_benchmark` diagnosis observations (normally at
least two trials per 22 scenarios) with `STATISTICAL_RECORDING`; then wire the
diagnosis artifact and config fingerprint. Resolving trials can subsequently
produce remediation calibration.

A gated trial is now scored on what it showed: #53 carries `severity_hit`
and `structured_grade` through the unresolved path, #54 stops `_remediation`
grading read-only `inspect` steps as bad remediation. On smoke2 the pair moves
scored criteria from 4 of 8 to 6, `severity_accuracy` from `None` to 1.0, and
remediation from FAIL to PASS — while a gated trial still seeds no remediation
calibration. #28 is unblocked.

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

The local runbook corpus snapshot is pre-publish and stale; do not use it to
describe current Notion content without regenerating it deliberately.

## Relevant files
- Query/context: `sre_agent/{namespace_scope,mcp_tool_wrapper,agent_nodes,
  context_compaction,narrative,audit_context}.py`, `config/agent_config.yaml`.
- GitHub payload: `edge_mcp_servers/mcp_servers/github_real/{payload,server}.py`.
- Benchmark: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench}.py`,
  `benchmarks/datasets/v2/`.
- Operations and rationale: `docs/ai/HANDOFF_CODEX.md`, `DECISIONS.md`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → 1,975 passed, 37 warnings.
- `scripts/audit_runbook_coverage.py` → 22/22 against the fresh live dump.
- `scripts/audit_runbook_controls.py` → 4 detected, 1 known blind.
- `check_python_quality.sh`, `check_eval_smoke.sh`, secret scan → pass.

## Known blockers or risks
- **The deployed image is stale and unidentifiable.** Worker preflight reports
  `code_sha=bc18e30-dirty-p0p1fix` — 11+ commits behind the branch, and built
  from an uncommitted tree, so what is running cannot be recovered from the
  sha. Any component "verified" against the live stack is verified against
  code nobody can name. Rebuild before believing a component check.
- `_scope_query` injects tenant namespace into only the first selector block;
  multi-selector PromQL remains incompletely scoped.
- Grades are not comparable across the #54 fix. The rubric method was renamed
  to `typed_remediation_action_match`, moving its sha256, so smoke2's earlier
  recorded grade belongs to the old semantics. Nothing paid has run yet, so
  nothing else is affected.
- The `graph_builder.py` half of #53 is committed but needs an image rebuild to
  affect live runs; `scoring.py` takes effect on the next scoring pass.
- `/app/reports` is not a volume; copy accounting/traces out before rebuilds.
- Never stage `.agents/` or `.env.local-backup-20260910`; never `git add -A`.

## Next bounded task
The user set the order: every backend component working as intended, then
the frontend wired, then benchmarking. **(0)** Rebuild and redeploy — the
running image is stale, and this needs the user's go-ahead. **(1)** Run
`pytest` on the Mac; the merged tree plus #53/#54 has executed nowhere and
the Codespace has no pytest (expect 1,982, up 7). **(2)** Verify components
against the rebuilt stack: `remediation_gate_approvals` empty against 61
`approval_requests`, the Slack credentials' source unexplained, a litellm
fallback on every model call, `STATISTICAL_RECORDING` never run here.
**(3)** Frontend: no restart policy on the dashboard, six API modules with no
caller. **(4)** Only then the campaign shape, ~$88 versus the $194+ tier, and
not without the user's stage and budget.

`docs/ai/HANDOFF_CODEX.md` → "The current plan" holds the evidence, the sweep
numbers and the trap for each step.
