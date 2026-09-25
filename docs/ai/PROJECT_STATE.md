# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Benchmarking, sized to the budget.** Backend correctness and console wiring
are both closed; the coverage preflight is measured. What remains is deciding
what to spend.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. A successful action is not
  replayed after process death; a cleared incident cannot restart remediation.
- **A verdict from `evaluate_action` cannot be appealed.** `policy_gate.decide`
  returns BLOCKED before the approval ladder runs, so bans belong in `decide`,
  judged from measured state — never the planner's `risk_level`, untrusted
  writer input.
- **Org scope is not cluster scope.** Tenant isolation holds, but
  `/ws/insights`, `/ws/incidents` and every `/incidents/{id}` route serve the
  whole org; narrowing to one cluster is the consumer's job.
- **A tool result must state which question it answered:** `evaluated_at` from
  Prometheus's own sample stamp, a deployment spec declaring itself startup
  defaults rather than running state.
- Recovery is the scenario's Prometheus probe, never incident status. Benchmark
  evidence is content-addressed; inconsistent evidence blocks a claim.
- **Slack is the action surface.** Approve, deny, acknowledge and mark-resolved
  are Slack-thread commands by design; the console observes and configures.

## Completed or verified work
- v2: 22 scenarios, recovery oracles, structured grading, adversarial cases,
  paired statistics, four ablation arms, a content-addressed release gate.
- **Six authorized trials, all `inventory_slow_queries`, ≈$1 each.** 1–5
  UNRESOLVED; trial 6 `VERIFIED_RECOVERED`, MTTR 958s, root-cause, remediation,
  severity and safety 100%, one harness approval, oracle confirming
  independently.
- **Three evidence-quality defects closed — `283f9ba`, deployed, parity proven.**
  `evaluated_at` on every metric read; `get_deployment_spec` labels its values
  STARTUP DEFAULT, not running state; the narrator may not write "Unknown"
  once the reflector has evidence or a causal chain.
- **Console wiring closed.** 19/19 pages handle loading, error and empty; no
  dead links, no mock data. Incidents starts an investigation via
  `POST /clusters/{id}/trigger`, no severity preselected (the API refuses to
  default it); the incident page loads `GET /incidents/{id}/logs` on demand.
  `POST /clusters/{id}/jobs/trigger` and `crud.create_job` are deleted. Every
  remaining uncalled route is deliberate.

## Active problem
Benchmarking, budget-blocked: the $88–$632 tiers are refused and six single
trials (~$6) are all that has been spent. Coverage is now measured. The skill
corpus holds 5 verified skills — crashloop ×2, latency, dependency, oom — and
no `high_error_rate` one, so `no_memory` is blind on exactly the
`high_error_rate` scenarios: 3/6 on dev, 3/4 on holdout. The incident
collection holds 0 points, so half of what that arm removes is already absent
from every arm. Seeding (#34) is a prerequisite for #28, not a parallel task.
Separately, specialists still exhaust the six-turn limit.

## Relevant files
- Evaluation: `benchmarks/{sre_bench,structured_grading,statistical_eval}.py`,
  `benchmarks/ablation_coverage.py`, `sre_agent/skill_store.py`.
- Tool scope: `edge_mcp_servers/mcp_servers/{prometheus_real,k8s_real}/server.py`,
  image-baked; rebuild their compose services, the deploy script misses them.
- Console: `dashboard/app/(dashboard)/clusters/[id]/*/page.tsx`;
  `dashboard/lib/console.ts` is types and formatters only.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2389 passed, 6 skipped** (2026-09-23).
- `benchmarks/calibrate_semantic_floor.py` → free, no LLM; fails if
  `_SEMANTIC_MATCH_FLOOR` leaves the measured gap (0.764, 0.851).
- `benchmarks/ablation_coverage.py --split {dev,holdout} --organization-id …
  --cluster-id …` → free. Needs `SKILL_STORE_PATH`; the live store is a docker
  volume, so `docker cp sre-agent-api:/app/data/skills.json` first. 2026-09-25:
  PARTIAL 3/6 dev, 3/4 holdout, `recall_possible=false`. Semantic and
  keyword-only agree now, so #59's floor closed the old 6/6-vs-3/6 divergence.
  Artifacts: `reports/coverage/`.
- `scripts/check_python_quality.sh` → ruff critical, mypy, compileall clean.
- `scripts/deploy_agent_runtimes.sh` → `code_sha=283f9ba`, 162 files, parity
  passed. `benchmarks/` is not in the image; `sre_agent/` is.
- The dashboard is image-baked too (no bind mounts) despite `next dev`:
  `cd platform && docker compose build dashboard && docker compose up -d
  --no-build --force-recreate dashboard`. Gate: `npm run lint` and
  `npm run build` (typechecks); `dashboard/` has no test framework.
- Trial 6: `reports/approve-20260923-inventory-slow/`. Rerun shape —
  `BENCH_SCENARIOS=inventory_slow_queries BENCH_AUTO_APPROVE=1`,
  `BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`.

## Known blockers or risks
- **Calibration needs ~100 more paid trials**; the corpus holds 2 against
  `minimum_samples=100`. Records group by task, not scenario, so N runs of one
  scenario clear the floor while describing one fault.
- Structured grading returns `INCOMPLETE`: `causal_chain` and
  `evidence_support` sit at `REQUIRES_CALIBRATION`, no blinded judge installed.
- Specialists still hit the six-turn investigation limit.
- Defect (c) is unit-verified only — never observed under live fire.
- Incident recall is provably inert — the collection holds 0 points. Dataset v3
  is not runnable, the Meridian image lacking the `metrics_enabled` rebuild.
  The corpus stays v2's 22.
- **No paid run beyond trial 6 is authorized.** The $88–$632 tiers stay refused.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Decide the benchmarking shape the budget allows, now that coverage is known.
Two sub-decisions: whether #34 seeds the missing `high_error_rate` skills by
paid train investigations (~$12) or deterministically from known remediations
($0, but the claim weakens from "the loop learns" to "memory helps"); and which
arms of #28 earn a trial — `full` vs `no_memory` on holdout is 8 trials (~$8).
No paid run beyond trial 6 is authorized until that decision is made.
