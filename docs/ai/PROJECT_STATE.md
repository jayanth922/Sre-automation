# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Benchmarking, sized to the budget.** Backend correctness, console wiring
and memory coverage are all closed. What remains is one spending decision
(#28) and one grading-quality decision (#64).

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
  paired statistics, four ablation arms, content-addressed release gate.
- **Six authorized trials, all `inventory_slow_queries`, ≈$1 each.** 1–5
  UNRESOLVED; trial 6 `VERIFIED_RECOVERED`, MTTR 958s, all four scalar hits,
  one harness approval, oracle confirming independently.
- **Three evidence-quality defects closed — `283f9ba`, deployed, parity
  proven:** `evaluated_at` on every metric read, `get_deployment_spec` labels
  values STARTUP DEFAULT, the narrator cannot write "Unknown" over evidence.
- **Console wiring closed.** 19/19 pages handle loading, error and empty; no
  dead links or mock data. Incidents triggers via `POST /clusters/{id}/trigger`
  with no severity preselected. `jobs/trigger` and `crud.create_job` are
  deleted; every remaining uncalled route is deliberate.
- **#34 seeding closed, two paid train trials ≈$2.** Skill store 5 → 7, adding
  `high_error_rate` for checkout-service and payment-service, both
  `verified_success`. `bad_deploy_checkout` MTTR 804s (rc✗ rem✓ sev✓ safe✓);
  `payment_charge_error_spike` MTTR 673s (rc✓ rem✓ sev✓ safe✓).
- **#63 closed:** an ISO 8601 interval stamp (`<start>/<end>`) voided
  temporal_reasoning — `_temporal` gives up on the first entry it cannot read,
  so two interval stamps discarded six good ones. Grader and producer now both
  anchor on the start instant. Fixing only the grader is worse: replay turns
  INSUFFICIENT_EVIDENCE into a spurious "not chronological" FAIL.

## Active problem
Benchmarking, budget-blocked: the $88–$632 tiers are refused; ≈$8 is spent
(six single trials plus two seeding trials). Coverage is no longer the
constraint — #34 is closed and dev/holdout are fully COVERED, so no arm is
blind. Grading is: 2 of 8 structured criteria sit at `REQUIRES_CALIBRATION`
with no judge installed, and the reflector returns an empty evidence list in
5 of 10 recorded trials (#64), voiding 2 more in about half of runs. The
incident collection still holds 0 points, so half of what `no_memory` removes
is already absent from every arm. #28 is scoped and NOT authorized.

## Relevant files
- Evaluation: `benchmarks/{sre_bench,structured_grading,statistical_eval}.py`,
  `benchmarks/ablation_coverage.py`, `sre_agent/skill_store.py`.
- Tool scope: `edge_mcp_servers/mcp_servers/{prometheus_real,k8s_real}/
  server.py`, image-baked; rebuild their compose services — the deploy script
  misses them.
- Console: `dashboard/app/(dashboard)/clusters/[id]/*/page.tsx`;
  `dashboard/lib/console.ts` is types and formatters only.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2389 passed, 6 skipped** (2026-09-23).
- `benchmarks/calibrate_semantic_floor.py` → free, no LLM; fails if
  `_SEMANTIC_MATCH_FLOOR` leaves the measured gap (0.764, 0.851).
- `benchmarks/ablation_coverage.py --split {dev,holdout} --organization-id …
  --cluster-id …` → free. Needs `SKILL_STORE_PATH`; the live store is a docker
  volume, so `docker cp sre-agent-api:/app/data/skills.json` first. 2026-09-25
  post-seeding: **COVERED 6/6 dev, 4/4 holdout**, every row
  `signature_match=yes`. Strength splits 6 same-service priors (≥0.80) and 4
  cross-service class transfers at exactly 0.50 — `match_score`'s admission
  threshold, which is `find_matching`'s `threshold=0.5`, NOT the 0.80
  `_SEMANTIC_MATCH_FLOOR` (that one grades cosines, on the semantic path only).
  Artifacts: `reports/coverage/`.
- `scripts/check_python_quality.sh` → ruff critical, mypy, compileall clean.
- `scripts/deploy_agent_runtimes.sh` → `code_sha=283f9ba`, 162 files, parity
  passed. `benchmarks/` is not in the image; `sre_agent/` is.
- The dashboard is image-baked too (no bind mounts) despite `next dev`:
  `cd platform && docker compose build dashboard && docker compose up -d
  --no-build --force-recreate dashboard`. Gate: `npm run lint` + `npm run
  build`; `dashboard/` has no test framework.
- Single-trial shape: `BENCH_SCENARIOS=<id> BENCH_AUTO_APPROVE=1
  BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`,
  statistical recording unset (`BENCH_SCENARIOS` refuses to run beside it).

## Known blockers or risks
- **Calibration needs ~100 more paid trials** against `minimum_samples=100`;
  the corpus holds 2. Records group by task, not scenario, so N runs of one
  scenario clear the floor while describing a single fault.
- Structured grading can never return `PASS`: `causal_chain` always sits at
  `REQUIRES_CALIBRATION` with no blinded judge installed, and `PASS` requires
  every criterion in {PASS, NOT_APPLICABLE}. Read the per-criterion states and
  the scalar hits for #28, never `overall_status`.
- The reflector emits an empty evidence list in 5 of 10 recorded trials,
  independent of the turn limit (#64); `causal_chain` is always populated.
  That voids evidence_support and temporal_reasoning together.
- 3 of 22 scenarios expect deploy/commit/rollback evidence no v2 injection
  creates — the only adapter toggles `/admin/config`. Arm-independent, but it
  caps dev root-cause accuracy at 5/6.
- Specialists routinely hit the six-turn limit. Verified deliberate (#62):
  env-tunable, evidence-preserving, constant across arms.
- Incident recall is provably inert — the collection holds 0 points. Dataset
  v3 is not runnable (the Meridian image lacks the `metrics_enabled` rebuild),
  so the corpus stays v2's 22.
- **No paid run beyond #34's two seeding trials is authorized.** #28 is not
  approved; the $88–$632 tiers stay refused.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Get a decision on #28: `full` vs `no_memory` on holdout, 8 trials ≈ $8 — the
smallest run that yields a real memory result. Before spending, settle #64:
whether to require `evidence` in the reflector schema so evidence_support and
temporal_reasoning grade in every trial instead of half. That changes agent
behaviour, so it needs a deploy and re-validation. #63 is fixed but
uncommitted and undeployed — `sre_agent/` is image-baked, so its producer half
does not reach live trials until deployed.
