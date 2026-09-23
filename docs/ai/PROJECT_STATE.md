# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**First measured recovery.** Trial 6 (2026-09-23) is the first the Prometheus
oracle verified as recovered. The two defects that made RESOLVED unreachable —
a policy rule no human could appeal, and a benchmark that discarded its own
calibration evidence — are closed and confirmed under live fire. What remains
is corpus size, not correctness.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- **A verdict from `evaluate_action` cannot be appealed.** `policy_gate.decide`
  returns BLOCKED before the approval ladder runs, and `_act_gate_node` builds
  the ACT report before looking up the approval. Environment and action bans
  therefore belong in `decide`, judged from measured state — never on the
  planner's own `risk_level` string, which is untrusted writer input.
- Specialist reads are bounded; verification stays deterministic. Approval
  cannot override a hard block.
- **Org scope is not cluster scope.** Tenant isolation holds
  (`event_visible_to_org` fail-closed, `get_owned_incident` joins on org), but
  `/ws/insights`, `/ws/incidents` and every `/incidents/{id}` route serve the
  whole org; narrowing to one cluster is the consumer's job.
- Benchmark evidence is content-addressed to scenario, code, config, rubric,
  model and traces; inconsistent evidence blocks a claim. Release needs
  recovery, quality, safety and cost traces — diagnosis authorizes nothing.
- Recovery is the scenario's Prometheus probe, never the incident status.
  `TERMINAL_APPLICATION_STATUSES` only ends polling early.
- **A trial writes two independent artifacts.** A trial row means something
  only against its pair in another arm and stays gated on the four `BENCH_*`
  experiment vars; a confidence observation is one reliability point and
  records on any live run.

## Completed or verified work
- v2: 22 scenarios, recovery oracles, structured grading, adversarial cases,
  paired statistics, four ablation arms, a content-addressed release gate.
- Memory preflight, all three v2 splits, semantic: 22/22 retrieve a skill, 12
  one of their own failure class, **4** one matching class *and* service. Zero
  tenant incident-memory points.
- **Six authorized trials, all `inventory_slow_queries`, ≈$1 each.** 1–5
  UNRESOLVED; trial 6 `VERIFIED_RECOVERED`, MTTR 958s, root-cause /
  remediation / severity / safety 100%, `false_resolved=False`, one harness
  approval. Path: plan → `requires_approval` → `POST /approve` → `applied 4 of
  4 live remediation(s)` → `verification → RESOLVED`, the oracle confirming
  independently on `inventory_db_p90_latency`.
- `reports/sre-bench-confidence.jsonl` now exists — diagnosis 0.72/True,
  remediation 0.45/True, both `live_benchmark`. First samples since 2026-09-21.
- Specialist ReAct is cost-bounded (six model turns, 48 calls, 120s, 3,000
  output tokens/turn) and keeps partial evidence at every limit. A runbook's
  own PromQL reaches the metrics specialist as text, no model call.
- Console audit closed the frontend milestone: 19/19 pages handle
  loading/error/empty, no dead links, clean `next build`. The chat surface is
  gone; Slack serves it.

## Relevant files
- Policy: `sre_agent/{policy_engine,policy_gate,act_phase}.py` — Rule 1 removed,
  with a comment recording why it cannot return.
- Logs: `edge_mcp_servers/mcp_servers/loki_real/server.py` (image-baked in
  `mcp-loki`; rebuild its compose service, the deploy script misses it).
- Evaluation: `benchmarks/{sre_bench,structured_grading,statistical_eval,
  ablation_eval,ablation_coverage}.py`; `docs/ai/AI_RESULTS.md` holds the
  public negative evidence.
- Retrieval: `sre_agent/skill_store.py`. Evidence acquisition:
  `sre_agent/{runbook_queries,runbook_probe}.py`,
  `agent_nodes.BaseAgentNode.__call__`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2345 passed, 6 skipped** (2026-09-23).
- `scripts/check_python_quality.sh` → ruff critical, mypy (3 curated files) and
  compileall all clean.
- `scripts/deploy_agent_runtimes.sh` refuses a dirty tracked tree, builds
  `sentinel/api:local` once, recreates api + temporal worker, proves parity.
  Last: `code_sha=2b73497`, 162 files. `benchmarks/` is **not** in the image
  (harness changes need no redeploy); `sre_agent/` is.
- Trial 6: `reports/approve-20260923-inventory-slow/`. Rerun shape —
  `BENCH_SCENARIOS=inventory_slow_queries BENCH_AUTO_APPROVE=1`,
  `BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`.

## Known blockers or risks
- **Calibration needs ~100 more paid trials.** The corpus holds 2 records
  against `minimum_samples=100` and `minimum_threshold_support=40` at
  `required_wilson_lower=0.90`. Records group by task, not scenario, so N runs
  of one scenario clear the floor while describing one fault — spread the
  corpus before trusting any threshold built from it.
- Structured grading returns `INCOMPLETE`: `causal_chain` and
  `evidence_support` sit at `REQUIRES_CALIBRATION`, no blinded judge installed.
  The four scored criteria pass.
- Specialists still hit the six-turn investigation limit — Performance Metrics
  and Application Logs in trial 6, GitHub in trial 5.
- Three evidence-quality defects from trial 5 remain: a Prometheus call omits
  `time`; the fault-injection knobs read as disabled while the injected delay is
  demonstrably running; `summary_text` says "root cause: Unknown" over a
  structured evaluation holding a correct diagnosis and 12 evidence entries.
- The logs lane is fixed end to end: tool, repo runbooks, and the Notion corpus
  the agent actually reads. No LogQL selector in the live corpus names `app`;
  the `kubectl -l app=` selectors that remain are correct, since pods do carry
  that label.
- `propose_skills` declares `threshold=0.5` on `match_score`'s additive scale
  but `SemanticSkillStore` compares it to a *cosine* similarity, so the
  semantic path admits unrelated skills. A floor needs calibration data.
- Incident recall is provably inert: half of what `no_memory` removes is
  already absent from every arm.
- Dataset v3 is not runnable — the Meridian image lacks the `metrics_enabled`
  rebuild. The corpus is v2's 22.
- **No paid run beyond trial 6 is authorized.** The $88/$194/$389/$583/$632
  tiers stay refused.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Close the three remaining evidence-quality defects — all free, all in the
acquisition lane — then frontend wiring, per the standing directive that backend
correctness comes first and benchmarking last. Any further paid trial needs
fresh authorization.
