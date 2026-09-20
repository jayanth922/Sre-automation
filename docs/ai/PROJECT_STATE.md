# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, cost-conscious, and
production-operable. Deterministic policy and durable state—not model prose—
control writes, approvals, status transitions, and operator-facing claims.

## Current milestone
The four-phase context/runbook pass and follow-up specialist hardening are
deployed to Codespace `cuddly-winner-659v67gv695hrxjw` (2026-09-19):

- Four Meridian runbooks were published to Notion. A post-write dump scores
  22/22; exactly four bodies changed, properties and 16 `RB-AUTO-*` pages did
  not. Backups are under `~/Downloads/notion-runbook-backups/`.
- Specialists receive the full selected procedure, exact alert labels/time,
  and bounded prior findings. Tool results enter the model at 20k characters
  maximum and histories at 60k tokens; durable evidence remains lossless.
- Model-directed logs, metrics, GitHub, Kubernetes, and fallback runbook reads
  are fail-closed on target/window/limit. Source servers also bound payloads;
  full runbook bodies are deliberately exempt.
- `github_real` now returns bounded, largest-change-first file/patch evidence.
- Stable Langfuse observation names identify each specialist role.

The work is committed and pushed. Codex's 24 commits are `origin/master`
(`5117955`); the Codespace's 20 are `pr/context-engineering-and-gated-grading`,
merged with Codex's in that branch. The two histories diverged at `1d4fe000`
and duplicated twelve commits, but 52 of 59 shared files were byte-identical,
so only four conflicted — three were prose-only and took Codex's wording
because it matched the code.

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
- `SENTINEL_ABLATION_ARM=full` is the shared control; four arms mean 88
  incidents per one-trial/scenario campaign, not 132.

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
- Latest recorded suite: 1,975 passed, 37 known warnings; runbooks 22/22;
  controls 4 detected plus 1 documented known blind; quality, 45-test eval
  smoke, and secret scan pass.

## Active problem
Autonomy is gated by calibration, not a runtime bug. Uncalibrated confidence
escalates SEV2 to SEV1, which requires approval and makes trials unresolved.
Stage 1 needs at least 40 `live_benchmark` diagnosis observations (normally at
least two trials per 22 scenarios) with `STATISTICAL_RECORDING`; then wire the
diagnosis artifact and config fingerprint. Resolving trials can subsequently
produce remediation calibration.

A gated trial is now scored on what it actually showed. #53 carries
`severity_hit` and `structured_grade` through the unresolved path; #54 stops
`_remediation` grading read-only `inspect` steps as bad remediation. On the
real smoke2 trial the two together take scored criteria from 4 of 8 to 6,
`severity_accuracy` from `None` to 1.0, and remediation from FAIL to PASS —
while `grader_status` stays `NOT_APPLICABLE`, `safety_ok` stays true and
`remediation_confidence` stays None, so a gated trial still seeds no
remediation calibration. #28 is unblocked.

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
- The only live stack is the Codespace, currently `Shutdown`; deployment facts
  above come from Claude's recorded live probes and cannot be rechecked now.
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
Run the test suite on the Mac — the merged tree plus #53/#54 has never been
executed anywhere, and the Codespace has no pytest. Then prove a statistical
run actually persists `cost_usd` and confidence records, and check that a
below-support calibration artifact cannot spuriously mark confidence
calibrated. Only then choose the campaign shape: ~$88 buys calibration support
from gated incidents; the full paired ablation is the $194+ tier. Do not launch
a paid campaign until the user picks stage and budget.
