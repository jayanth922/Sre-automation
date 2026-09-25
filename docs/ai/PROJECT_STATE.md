# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**All known defects fixed in one batch, then one run.** Two paid campaigns have
run (#28 on 2026-09-24, its re-run #70 on 2026-09-25). Each surfaced harness
defects that cost paid trials, so the standing decision (user, 2026-09-25) is
to stop verifying fixes one at a time and land every open fix before any
further paid run. Fixes are verified offline — unit tests plus replay of
already-recorded campaign data — which costs no agent API credits at all; only
#72 needed a live incident.

## Current architecture and invariants
- **Layout (PR #56, 2026-09-25):** production packages in `src/`, the console
  in `apps/dashboard/`, MCP and backend services in `services/`, evaluation in
  `evals/benchmarks/`, deployment assets in `infra/`, the Meridian reference
  integration in `examples/meridian/`. `pyproject.toml` sets
  `pythonpath = ["src", "evals"]`, so `import sre_agent` still resolves.
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
- **Two paid campaigns, `full` vs `no_memory` on holdout v2, 8 trials each:
  #28 (≈$8, `reports/ablation-20260924/`) and its re-run #70
  (`reports/ablation-20260925/`).** Both **NOT_DEMONSTRATED**, never REFUTED;
  both reached only 2 valid pairs, on which every graded delta is exactly zero.
  Arm separation is proven, not asserted: of 326 leaf configuration fields
  across the arms' manifests exactly one differs (`runtime.ablation_arm`).
- **#65–#75 all fixed, committed and pushed** (`3405d00`). Rationale lives in
  the docstrings and the commit messages; each has tests that fail against the
  pre-fix file. Four results from that work outlive the diffs:
  - #70's reported "MTTR mean 526s" was an artifact — two real recoveries at
    786.1s averaged with a 7s poll interval. Do not quote the published figure.
  - `payment_subthreshold_charge_errors` is a deliberate negative control, not
    a mis-set threshold. Its scenario block is correct as written.
  - Incident-memory promotion now happens inside `graph_builder._act_gate_node`
    (#72), which is what makes the recall half of memory testable at all.
  - Both paid campaigns' `cost_usd` figures are low: the trials that took no
    action had their real cost discarded (#73). Re-derive, don't reuse.

## Active problem
None open. #73, #74, #75 and #4 are fixed, verified offline and pushed. Three
findings from that batch are not yet tracked defects:
- `trace_evidence_artifact` on every trial points at `reports/run-trace.jsonl`,
  a rolling shared path that later runs overwrite, so content-addressed trace
  evidence does not survive the campaign that produced it. The recorded digest
  stays valid; the file it names does not.
- `expected_evidence` is dead data — 22 scenarios x 2-4 hand-written
  assertions, loaded, validated, set on `ScenarioSpec`, read by nothing outside
  tests. `root_cause_keywords` is worse: `scoring.py:35` calls it an "any-of
  match against the summary" and no such match exists; its only reader is
  `retrieval_eval.py:471`, building a query string. Evidence quality is scored
  only by `evidence_support`, a `_semantic_criterion` stuck at
  REQUIRES_CALIBRATION — so in practice it is not scored at all.
- `train/bad_deploy_checkout` cannot be diagnosed correctly and is deliberately
  left that way; see `datasets/v2/COVERAGE.md`. Retagging it to an observable
  fault mode would buy a passing trial by deleting the record that a deployment
  case is untestable here.

## Relevant files
- Evaluation: `evals/benchmarks/{sre_bench,structured_grading,statistical_eval,
  ablation_eval,recovery_oracle,ablation_coverage}.py`,
  `src/sre_agent/skill_store.py`, `evals/benchmarks/datasets/v2/holdout.json`,
  `src/sre_agent/api/v1/alerts.py` (`_fold_target`,
  `_FOLD_WINDOW_MINUTES = 120`).
- Tool scope: `services/edge_mcp_servers/mcp_servers/{prometheus_real,k8s_real}/
  server.py`, image-baked; rebuild their compose services — the deploy script
  misses them.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2477 passed, 6 skipped** (2026-09-25,
  after the #73/#74/#75/#4 batch). No `--timeout` plugin here. The Codespace
  `.venv` still has an editable install pointing at the deleted `sre_agent/`;
  pytest works anyway via `pythonpath = ["src", "evals"]`, but anything doing
  `python -c "import sre_agent"` outside pytest needs `.venv/bin/pip install
  -e .` (`uv` is not on PATH).
- `scripts/ci/check_python_quality.sh` → ruff critical, mypy, compileall clean.
- `evals/benchmarks/ablation_coverage.py --split {dev,holdout}` → free. Needs
  `SKILL_STORE_PATH`; the live store is a docker volume, so `docker cp
  sre-agent-api:/app/data/skills.json` first. 2026-09-25: **COVERED 6/6 dev,
  4/4 holdout**. Cross-service transfers sit at exactly 0.50 — `match_score`'s
  admission threshold, NOT the 0.80 `_SEMANTIC_MATCH_FLOOR` (cosines only).
- `evals/benchmarks/ablation_eval.py` → free. Exit 0 with `NOT_DEMONSTRATED`; 2
  is REFUTED.
- Ablation arm: `SENTINEL_ABLATION_ARM` is read at graph-build time inside
  `sre-agent-api`, so the arm belongs to the container, not the bench client.
  Append it to the root `.env`, then `docker compose -f
  infra/local/docker-compose.yaml --profile local-temporal up -d --no-deps
  sre-agent-api temporal-worker` (profile-gated worker). Confirm with
  `current_ablation().describe()` before spending.
- Single trial: `BENCH_SCENARIOS=<id> BENCH_AUTO_APPROVE=1
  BENCH_INCIDENT_TIMEOUT_SEC=2700`, secrets from `/home/vscode/bench.env`,
  recording unset (`BENCH_SCENARIOS` refuses to run beside it).
- `evals/` is not in the agent image; `src/sre_agent/` is. The dashboard is
  image-baked: rebuild, then `up -d --no-build --force-recreate`.

## Known blockers or risks
- **No further paid run is authorized**, and none should be proposed until the
  whole batch above is landed. The $88–$632 tiers stay refused. Measured basis
  for any future estimate: 4 priced trials, mean $0.895, range $0.825–$1.001 —
  but see #73, these lean low. Costed only: 8-trial re-run with memory seeded
  ≈$18; the 20-pair campaign §9 calls conclusive ≈$47 (range $40–60), which on
  a 4-scenario holdout means 5 repetitions each, measuring run-to-run variance
  more than scenario breadth.
- The re-run is **not poolable** with #28. The agent is byte-identical, so the
  fingerprint and `dataset_sha256` still match and attestation passes — but the
  harness producing the measurements changed. Report it as a new experiment.
- `holdout` is `frozen: true` in `dataset.json`. Appending to it would
  invalidate both campaigns' attestations; a wider holdout means a declared
  v3 split, not an edit. `dev` and `train` are unfrozen and may grow.
- **Calibration needs ~100 more paid trials** against `minimum_samples=100`;
  the corpus holds 2.
- Structured grading can never return `PASS`: `causal_chain` sits at
  `REQUIRES_CALIBRATION` with no blinded judge. Read per-criterion states and
  scalar hits. In #28, 6 of 8 rows carry `structured_failure`.
- 3 of 22 scenarios expect deploy/commit/rollback evidence no v2 injection
  creates — the only adapter toggles `/admin/config`. Arm-independent; caps dev
  root-cause accuracy at 5/6.
- Specialists routinely hit the six-turn limit. Verified deliberate (#62).
- Incident recall was inert in **both** arms of both campaigns — 0 points in
  the collection — so neither measured the recall half of memory. Both
  attestations say so and return NOT_DEMONSTRATED; do not restate either as a
  null result. #72 makes the writes work, so a seeded re-run would finally put
  that half under test. Dataset v3 is not runnable (the Meridian image lacks the
  `metrics_enabled` rebuild).
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Decide what to do about the three untracked findings under Active problem. The
first two are free and self-contained: give `expected_evidence` a reader or
delete it, and stop pointing every trial's `trace_evidence_artifact` at one
rolling path. Neither needs a paid run. Only after the queue is empty should a
paid run be discussed.

Invariants the #73/#74/#75/#4 batch set (detail is in the commits, not here):
- **Span completeness and cost completeness are separate questions (#73).** A
  run that correctly took no action emits no approval/mutation/verification
  span, so act-path kinds are required only of a run that entered the act path,
  and `cost_usd` is gated on the cost accounting's own flag. `statistical_eval`
  carried the same coupling; a cost may still never arrive without the trace
  evidence that priced it. Same defect class as #69.
- **A benchmark asserts the precondition it controls and records the
  contamination it cannot (#74).** Same-service folding is correct production
  behaviour (12/12 in shadow mode), wrong only in the benchmark's shape, so the
  fix is harness-side: settle open incidents before firing, tag observed folds
  `cross_scenario_fold`. The trial schema is closed — reasons go to the notes.
- **"Nothing waiting" and "I could not tell" are different answers (#75).**
- **Incident-type breadth is blocked on the Meridian app, not the dataset
  (#4).** All 22 scenarios drive one adapter and seven `/admin/config` knobs —
  100% of the injection surface that exists. Holdout, where both paid campaigns
  ran, is 4 scenarios covering 4 of 11 categories. See
  `evals/benchmarks/datasets/v2/COVERAGE.md`.
