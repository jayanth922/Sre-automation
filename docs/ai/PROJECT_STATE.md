# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes, approvals,
status transitions and operator-facing claims.

## Current milestone
**Benchmark preflight — closed.** Frontend is closed. The free checks now say
what a four-arm ablation would measure: little, for reasons upstream of the
arms.

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
  writes a content-addressed manifest — but one packaged case is not a
  dataset, and semantic criteria still lack two labelers and agreement.
- Memory preflight, all three v2 splits, semantic, on the agent's own
  interpreter: 22/22 scenarios retrieve a skill, 12 one of their own failure
  class, **4** one matching class *and* service. Zero tenant incident-memory
  points. Artifacts: `reports/ablation-memory-coverage-v2-*-20260922.json`.
- One authorized smoke, no retry: `inventory_slow_queries`, full arm,
  `investigated`/`UNRESOLVED`, diagnosis `FAIL`, 1,540.78s, **$2.5263** over 97
  model calls. Four defects found, all fixed. Non-comparable, but it is the
  live per-trial price: budget $2.53 and 26 minutes, not $2.21.
- Specialist ReAct is cost-bounded — six model turns each, a 48-call ceiling,
  a 120s wall clock, and 3,000 output tokens per turn. Every limit keeps
  partial evidence, the wall clock included. See DECISIONS.
- A runbook's own PromQL reaches the metrics specialist as text, extracted
  without a model call. `get_golden_signals` is built from the cluster's one
  configured latency histogram and structurally cannot answer an alert whose
  signal is a different metric.
- Console audit closed the frontend milestone: no absent endpoints, no dead
  links, 19/19 pages handling loading/error/empty, clean `next build`. Five
  defects fixed and pinned by tests — two honesty, three cross-cluster
  renders. The chat surface is gone (`handle_incident_message` serves Slack).

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
- `.venv/bin/python -m pytest -q` → **2198 passed, 6 skipped** (2026-09-22).
- Preflight: `docker cp benchmarks sre-agent-api:/app/benchmarks`, then
  `/app/.venv/bin/python /app/benchmarks/ablation_coverage.py --split <s>
  --expect-retrieval-path semantic` (holdout: `BENCH_ALLOW_HOLDOUT=1`), then
  delete the copy.
- Ruff clean on every file changed; repo-wide counts unchanged from HEAD.

## Known blockers or risks
- **No trial has ever recovered — 0 of 12 recorded oracle rows**, so
  `recovery_success` and `quality_success` are constant zero and `mttr` is
  never computable. Both diagnosed causes are now closed in code and pinned by
  tests: the metrics lane is handed the runbook's own PromQL, and a lane cut
  off by the wall clock reports the evidence it already has instead of
  "no data". Closed is not measured — nothing has re-run, so 0/12 stands as
  the last observation and one trial decides whether recovery is reachable.
- `propose_skills` declares `threshold=0.5` on `match_score`'s additive scale;
  `SemanticSkillStore` compares that number against a *cosine* similarity, so
  the semantic path admits unrelated skills. Left unchanged: a defensible
  floor needs calibration data.
- Incident recall is provably inert: half of what `no_memory` removes is
  already absent from every arm.
- Dataset v3 is not runnable: `checkout-service/app.py` has the
  `metrics_enabled` switch but the Meridian image is not rebuilt. The corpus
  is v2's 22.
- **The toast fix is half-live** — stamping `cluster_id` is a producer change
  baked into the API image, so until a deploy the console shows no incident
  toasts rather than the wrong cluster's.
- No paid rerun or campaign is authorized; deploy is not either.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
One re-measured `inventory_slow_queries` trial (~$2-3, **not authorized**) —
the only way to learn whether the evidence fixes turn 0/12 into a recovery.
Everything cheaper is done. The same trial should also cost ~6% less: the
output ceiling clips 7 of its 85 specialist turns.
