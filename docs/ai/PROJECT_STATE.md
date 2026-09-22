# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes, approvals,
status transitions and operator-facing claims.

## Current milestone
**Evidence acquisition — fixed, unmeasured.** Frontend and preflight are
closed. The three defects the 2026-09-22 trial exposed are closed in code and
pinned by tests; nothing has re-run since.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- Specialist reads are bounded; verification remains deterministic. Approval
  cannot override a hard block.
- **Org scope is not cluster scope.** `event_visible_to_org` is fail-closed
  and `get_owned_incident` joins on org, so tenant isolation holds — but
  `/ws/insights`, `/ws/incidents` and every `/incidents/{id}` route serve the
  whole org. Narrowing to one cluster is the consumer's job.
- Benchmark evidence is content-addressed to scenario, code, config, rubric,
  model and traces; inconsistent evidence blocks a claim. Release needs
  recovery, quality, safety and cost traces — diagnosis authorizes nothing.
- Recovery is the scenario's Prometheus probe, never the incident status.
  `TERMINAL_APPLICATION_STATUSES` only ends polling early.

## Completed or verified work
- v2: 22 scenarios, independent recovery oracles, structured grading,
  adversarial cases, paired statistics, four ablation arms, a content-addressed
  release gate.
- Trial schema v3 and ablation report v2 separate diagnosis from recovery and
  quality; release evidence recomputes it. Dataset v3 ⊇ v2 by three.
- The blinded-calibration builder validates digests, hides provenance and
  writes a content-addressed manifest — but one case is not a dataset and
  semantic criteria still lack two labelers.
- Memory preflight, all three v2 splits, semantic, on the agent's own
  interpreter: 22/22 scenarios retrieve a skill, 12 one of their own failure
  class, **4** one matching class *and* service. Zero tenant
  incident-memory points.
- Two authorized trials, both `inventory_slow_queries`/`UNRESOLVED`: the
  2026-09-21 graded run ($2.53, 97 calls, 26 min) found four defects, all
  fixed; the 2026-09-22 run re-measured them (below).
- Specialist ReAct is cost-bounded — six model turns each, a 48-call ceiling,
  a 120s wall clock, 3,000 output tokens per turn. Limits must keep partial
  evidence; the recursion cap does not (below). See DECISIONS.
- A runbook's own PromQL reaches the metrics specialist as text, no model
  call. `get_golden_signals` uses the cluster's one configured latency
  histogram and cannot answer an alert keyed to a different metric.
- Console audit closed the frontend milestone: no absent endpoints or dead
  links, 19/19 pages handle loading/error/empty, clean `next build`, five
  defects fixed and pinned. The chat surface is gone; Slack serves it.

## Relevant files
- Evaluation: `benchmarks/{ablation_coverage,statistical_eval,sre_bench,
  ablation_eval}.py`; `docs/ai/AI_RESULTS.md` holds the public negative
  evidence and limitations.
- Retrieval: `sre_agent/skill_store.py` — `match_score`, `propose_skills`,
  `SemanticSkillStore._find_matching`.
- Evidence acquisition: `sre_agent/runbook_queries.py` →
  `narrative._runbook_query_hints_block`; partial-evidence retention and the
  soft deadline in `agent_nodes.BaseAgentNode.__call__`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2226 passed, 6 skipped** (2026-09-22),
  +28 for the three defect fixes. Ruff clean on every changed file.
- Preflight: `docker cp benchmarks sre-agent-api:/app/benchmarks`, then
  `/app/.venv/bin/python /app/benchmarks/ablation_coverage.py --split <s>
  --expect-retrieval-path semantic` (holdout: `BENCH_ALLOW_HOLDOUT=1`), then
  delete the copy.
- Ruff clean on every file changed; repo-wide counts unchanged from HEAD.
- 2026-09-22 trial, same dataset sha256 as the graded run →
  `reports/postfix-smoke-20260922-inventory-slow/`: **$1.08 vs $2.49, 40 model
  calls vs 97, 8.5 vs 26 min, still UNRESOLVED.**

## Known blockers or risks
- **No trial has recovered — 0 of 13 oracle rows**, so `recovery_success`
  and `quality_success` are constant zero and `mttr` is never computable.
  All three 2026-09-22 causes are closed, none re-measured. (a) Timing: the
  lane evaluated the runbook's `rate(...[5m])` at the alert stamp, set at
  fault injection, reading 0.0221s of pre-fault traffic.
  `sre_agent/runbook_probe.py` now runs the runbook's PromQL over
  `[alert-5m, now]` before the first model turn via the lane's bound,
  scoped, audited `get_metric_range` — no model call, fail-soft.
  (b) Relabelling: pass 2 swapped `job=` for `namespace`/`service` and
  matched nothing; the brief now forbids touching a matcher.
  (c) `GraphRecursionError` killed the logs lane twice: `pre_model_hook` is a
  node, so T turns need `3T-1` steps and the backstop allowed `2T+2`=14. Now
  `3T+2`, and a step ceiling keeps the partial-evidence digest.
- `propose_skills` declares `threshold=0.5` on `match_score`'s additive
  scale; `SemanticSkillStore` compares it against a *cosine* similarity, so
  the semantic path admits unrelated skills. A defensible floor needs
  calibration data.
- Incident recall is provably inert: half of what `no_memory` removes is
  already absent from every arm.
- Dataset v3 is not runnable: the Meridian image lacks the `metrics_enabled`
  rebuild. The corpus is v2's 22.
- No further paid run is authorized. The 2026-09-22 trial was the one the
  user approved; the $88/$194/$389/$583 tiers stay refused.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Deploy the three fixes and re-run one `inventory_slow_queries` trial
(~$1.10, **not authorized**) — the only way to learn whether measured
evidence turns 0/13 into a recovery. Everything free is done.
