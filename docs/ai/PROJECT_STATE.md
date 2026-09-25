# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Benchmarking, sized to the budget.** Backend correctness, console wiring and
memory coverage are closed. The first paid ablation campaign (#28) ran to
completion — both arms, 8 trials, ≈$8 — but two of its four pairs were
destroyed by harness defects. Those defects (#65, #66, #68) are now fixed and
tested. What remains is a ≈$8–10 re-run, which needs authorization.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. A successful action is not
  replayed after process death; a cleared incident cannot restart remediation.
- **A verdict from `evaluate_action` cannot be appealed.** `policy_gate.decide`
  returns BLOCKED before the approval ladder runs, so bans belong in `decide`,
  judged from measured state — never the planner's `risk_level`.
- **Org scope is not cluster scope.** Tenant isolation holds, but
  `/ws/insights`, `/ws/incidents` and `/incidents/{id}` serve the whole org;
  narrowing to one cluster is the consumer's job.
- **A tool result must state which question it answered:** `evaluated_at` from
  Prometheus's own sample stamp; a deployment spec labelled startup defaults.
- Recovery is the scenario's Prometheus probe, never incident status. Benchmark
  evidence is content-addressed; inconsistent evidence blocks a claim.
- **Slack is the action surface.** Approve, deny, acknowledge and mark-resolved
  are Slack-thread commands; the console observes and configures.

## Completed or verified work
- v2: 22 scenarios, recovery oracles, structured grading, adversarial cases,
  paired statistics, four ablation arms, content-addressed release gate.
- Console wiring closed: 19/19 pages handle loading, error and empty.
- #34 seeding (two paid train trials, ≈$2): skill store 5 → 7, adding
  `high_error_rate` for checkout- and payment-service, both `verified_success`.
- **#28 campaign (2026-09-25): `full` vs `no_memory`, holdout v2, 8 trials,
  ≈$8.** Arm separation proven, not asserted: of 326 leaf configuration fields
  compared across the arms' manifests, exactly one differs
  (`runtime.ablation_arm`); both carry `ablation_experiment=true` and
  `learned_memory_writes=false`. Verdict **NOT_DEMONSTRATED**, never REFUTED.
  On the two valid pairs every graded delta is exactly zero; the one non-zero
  diagnosis delta was #66's artifact. See `reports/ablation-20260924/`.
- **#65/#66/#67/#68 fixed, all four uncommitted.** #65: `_wait_for_recovery`
  broke on `tracker.recovered_at` alone, so an unarmed probe ended the trial
  ~7s in with 0 spans; it now also requires `tracker.failure_observed`. #66:
  teardown now closes the incident a trial opened (`mark-resolved`, the
  sanctioned path), so no orphan folds or dedups the next scenario's alert.
  #68: the declared `BENCH_CONFIG_FINGERPRINT` is checked against the first
  trial's manifest, so a bad declaration costs one trial, not a campaign. #67:
  `--memory-coverage` was routed through `load_manifest` and was unusable.
  Rationale lives in the docstrings; 11 tests in
  `tests/test_bench_trial_teardown.py`, which fail against the pre-fix file.
  Note for anyone re-reading #65: `payment_subthreshold_charge_errors` is a
  deliberate negative control, not a mis-set threshold. Its scenario block is
  correct as written.

## Active problem
Nothing technical blocks the re-run; it waits on a spend decision and a commit.
Secondary: clean scenarios (#69) still report `VERIFIED_RECOVERED` and
contribute a meaningless MTTR, because `OracleStatus` has no verdict for
"investigated and correctly did nothing". Deferred deliberately — that Literal
is consumed by `scoring.py`, `statistical_eval.py`, `make_release_fixtures.py`
and ~10 test files.

## Relevant files
- Evaluation: `benchmarks/{sre_bench,structured_grading,statistical_eval,
  ablation_eval,recovery_oracle,ablation_coverage}.py`,
  `sre_agent/skill_store.py`, `benchmarks/datasets/v2/holdout.json`,
  `sre_agent/api/v1/alerts.py` (`_fold_target`, `_FOLD_WINDOW_MINUTES = 120`).
- Tool scope: `edge_mcp_servers/mcp_servers/{prometheus_real,k8s_real}/
  server.py`, image-baked; rebuild their compose services — the deploy script
  misses them.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2389 passed, 6 skipped** (2026-09-23);
  bench subset after #65/#66/#68 → **222 passed** (2026-09-25).
- `scripts/check_python_quality.sh` → ruff critical, mypy, compileall clean.
- `benchmarks/ablation_coverage.py --split {dev,holdout}` → free. Needs
  `SKILL_STORE_PATH`; the live store is a docker volume, so `docker cp
  sre-agent-api:/app/data/skills.json` first. 2026-09-25: **COVERED 6/6 dev,
  4/4 holdout**. Cross-service transfers sit at exactly 0.50 — `match_score`'s
  admission threshold, NOT the 0.80 `_SEMANTIC_MATCH_FLOOR` (cosines only).
- `benchmarks/ablation_eval.py` → free. Exit 0 with `NOT_DEMONSTRATED`; 2 is
  REFUTED.
- Ablation arm: `SENTINEL_ABLATION_ARM` is read at graph-build time inside
  `sre-agent-api`, so the arm belongs to the container, not the bench client.
  Append it to the root `.env`, then `docker compose -f
  platform/docker-compose.yaml --profile local-temporal up -d --no-deps
  sre-agent-api temporal-worker` (profile-gated worker). Confirm with
  `current_ablation().describe()` before spending.
- Single trial: `BENCH_SCENARIOS=<id> BENCH_AUTO_APPROVE=1
  BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`,
  recording unset (`BENCH_SCENARIOS` refuses to run beside it).
- `benchmarks/` is not in the agent image; `sre_agent/` is. The dashboard is
  image-baked: rebuild, then `up -d --no-build --force-recreate`.

## Known blockers or risks
- **No further paid run is authorized.** #28's ≈$8 tier is spent; the $88–$632
  tiers stay refused. A clean 4-pair re-run costs ≈$8–10.
- The re-run is **not poolable** with #28. The agent is byte-identical, so the
  fingerprint and `dataset_sha256` still match and attestation passes — but the
  harness producing the measurements changed. Report it as a new experiment.
- **Calibration needs ~100 more paid trials** against `minimum_samples=100`;
  the corpus holds 2. Records group by task, not scenario.
- Structured grading can never return `PASS`: `causal_chain` sits at
  `REQUIRES_CALIBRATION` with no blinded judge. Read per-criterion states and
  scalar hits. In #28, 6 of 8 rows carry `structured_failure`.
- 3 of 22 scenarios expect deploy/commit/rollback evidence no v2 injection
  creates — the only adapter toggles `/admin/config`. Arm-independent; caps dev
  root-cause accuracy at 5/6.
- Specialists routinely hit the six-turn limit. Verified deliberate (#62).
- Incident recall is provably inert — 0 points in the collection. Dataset v3 is
  not runnable (the Meridian image lacks the `metrics_enabled` rebuild).
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Two decisions the work waits on: commit the four uncommitted files
(`benchmarks/sre_bench.py`, `benchmarks/ablation_eval.py`,
`docs/ai/PROJECT_STATE.md`, `tests/test_bench_trial_teardown.py`), and
authorize the ≈$8–10 re-run of `full` vs `no_memory` on holdout v2 for 4 clean
pairs. Then #69, which needs a vocabulary decision before any patch.
