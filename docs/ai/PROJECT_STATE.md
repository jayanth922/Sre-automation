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

Nothing is committed. HEAD is `b843823` on `master`; the Codespace runs the
dirty working tree under a synthetic `SENTINEL_CODE_SHA`.

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

The proposed diagnosis-only ablation needs a design fix before spending money:
`TrialRecord.quality_success` currently requires recovery + PASS + safety, and
unresolved trials must use `grader_status=NOT_APPLICABLE`. Persisting
`structured_grade` in the grader artifact alone does not feed diagnosis quality
into `statistical_eval` or `ablation_eval`; without a schema/evaluator change,
quality and recovery are both zero in every arm. At $2.21/incident, one, two,
and three trials per scenario are upper-bounded near $194, $389, and $583.

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
- Deployed code is uncommitted and the Codespace image is ahead of Git history.
- `/app/reports` is not a volume; copy accounting/traces out before rebuilds.
- Never stage `.agents/` or `.env.local-backup-20260910`; never `git add -A`.

## Next bounded task
Define and test a diagnosis-only paired metric/schema for unresolved ablation
trials, while preserving `grader_status=NOT_APPLICABLE`. Separately prove a
statistical run persists cost and confidence records. Do not launch a paid
campaign until the user chooses its stage and budget; commit/push also remain
unauthorised.
