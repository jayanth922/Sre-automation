# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Frontend wiring.** Backend correctness is closed for now: trial 6 was the
first recovery the Prometheus oracle verified, and the three evidence-quality
defects it exposed shipped in `283f9ba`. Per the standing directive the console
comes next, benchmarking last.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. A successful action is not
  replayed after process death; a cleared incident cannot restart remediation.
- **A verdict from `evaluate_action` cannot be appealed.** `policy_gate.decide`
  returns BLOCKED before the approval ladder runs, so bans belong in `decide`,
  judged from measured state — never from the planner's `risk_level`, which is
  untrusted writer input.
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
  independently on `inventory_db_p90_latency`.
- **Three evidence-quality defects closed — `283f9ba`, deployed, parity proven.**
  (a) `get_metric` / `get_golden_signals` return `evaluated_at` plus a warning
  when `time=` was omitted or unparsable. (b) `get_deployment_spec` states
  every value is a STARTUP DEFAULT: `inventory-service` *declares*
  `SLOW_QUERY_RATE="0"` while the adapter drives the live rate to 1.0 through
  `PUT /admin/config`, so trial 5 read a superseded value, not an absence.
  (c) `narrate_supervisor_summary` may not write "Unknown" once the reflector
  settled — settled meaning evidence or a causal chain, since `hypothesis` is
  required and proves nothing alone.
- **Console wiring closed.** 19/19 pages handle loading, error and empty; no
  dead links, no mock data. The incidents page starts an investigation
  (`POST /clusters/{id}/trigger`) with no severity preselected, because the API
  refuses to default it, and separates a new investigation from the endpoint's
  title-dedup match. The incident page loads the flight recorder
  (`GET /incidents/{id}/logs`) on demand: the transcript is the curated
  account, this the raw one. `POST /clusters/{id}/jobs/trigger` and
  `crud.create_job` are deleted: the row they wrote — PENDING, investigation,
  NULL payload — is what `claim_jobs` selects, so the worker claimed it and
  dead-lettered it. Every remaining uncalled route is deliberate.

## Active problem
Benchmarking, and it is budget-blocked: the $88–$632 tiers are refused, and six
single trials (~$6) are all that has been spent. Two defects stay unfiled —
`propose_skills` tests an additive `threshold=0.5` against a cosine score, and
specialists still exhaust the six-turn limit, capping how far one investigation
reaches before the reflector sees it.

## Relevant files
- Console: `dashboard/app/(dashboard)/clusters/[id]/*/page.tsx`.
  `dashboard/lib/console.ts` is types and formatters only — each page fetches.
- Tool scope: `edge_mcp_servers/mcp_servers/{prometheus_real,k8s_real}/server.py`,
  image-baked; rebuild their compose services, the deploy script misses them.
- Narrative: `sre_agent/narrative.py`, call site `sre_agent/supervisor.py:1370`.
- Evaluation: `benchmarks/{sre_bench,structured_grading,statistical_eval}.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2385 passed, 6 skipped** (2026-09-23).
- `scripts/check_python_quality.sh` → ruff critical, mypy, compileall clean.
- `scripts/deploy_agent_runtimes.sh` → `code_sha=283f9ba`, 162 files, parity
  passed. `benchmarks/` is not in the image; `sre_agent/` is.
- The dashboard is image-baked too (no bind mounts) despite running `next dev`:
  `cd platform && docker compose build dashboard && docker compose up -d
  --no-build --force-recreate dashboard`. Gate is `npm run lint` plus
  `npm run build`, which typechecks; `dashboard/` has no test framework.
- Trial 6: `reports/approve-20260923-inventory-slow/`. Rerun shape —
  `BENCH_SCENARIOS=inventory_slow_queries BENCH_AUTO_APPROVE=1`,
  `BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`.

## Known blockers or risks
- **Calibration needs ~100 more paid trials**; the corpus holds 2 records
  against `minimum_samples=100`. Records group by task, not scenario, so N runs
  of one scenario clear the floor while describing one fault.
- Structured grading returns `INCOMPLETE`: `causal_chain` and
  `evidence_support` sit at `REQUIRES_CALIBRATION`, no blinded judge installed.
- Specialists still hit the six-turn investigation limit.
- Defect (c) is unit-verified only — never observed under live fire.
- `propose_skills` compares an additive `threshold=0.5` against a cosine score,
  so the semantic path admits unrelated skills.
- Incident recall is provably inert; dataset v3 is not runnable, the Meridian
  image lacking the `metrics_enabled` rebuild. The corpus stays v2's 22.
- **No paid run beyond trial 6 is authorized.** The $88–$632 tiers stay refused.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Decide the benchmarking shape the budget allows: which arms of #28 earn one
trial each rather than the full four-arm matrix, and whether #34's memory
seeding can run against the train split without a paid investigation per item.
No paid run beyond trial 6 is authorized until that decision is made.
