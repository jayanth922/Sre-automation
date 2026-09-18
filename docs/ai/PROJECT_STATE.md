# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.
Deterministic policy and durable state—not model prose—must control writes,
approvals, status transitions, and operator-facing claims.

## Current milestone
Closing the HolmesGPT-comparison gaps (canvas
`sentinel-vs-holmesgpt.canvas.tsx`). All implementable items are done: audit
redaction (#16), the Anthropic-only provider contract (#17), token-aware
context budgeting (#18), two-part retrieval quality measurement (#19),
benchmark dataset v2 at 22 scenarios (#20), cost-derived autonomy thresholds
with enforced evidence provenance (#21), and the ablation harness (#22). What
remains is not code: none of #16-#22 has been deployed, and the ablation arms
have never been run against a live cluster, so the three architectural claims
are measurable but not yet measured.

Prior milestone, deployed and live-verified: P0 #4 crash-resumable live
remediation, with stable Langfuse names, fail-closed image parity,
missed-clear reconciliation, and bounded pre-claim retries.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant/namespace, idempotency, and audit boundary. Human approval
  cannot override a hard policy block; `EXECUTOR_LIVE=true` fails closed
  without Temporal and an incident ID.
- Failures before an idempotency claim are retryable
  (`MutationPreDispatchError`); after it, retry-unsafe. Three Temporal
  attempts; `ERROR`/unknown is terminal for the batch. After Alertmanager
  clear the next activity returns `REFUSED/incident_resolved` and stops
  scheduling, keeping completed results in Slack/timeline output.
- Lost resolved webhooks are recovered only when the alert job's Prometheus
  rule is healthy and two snapshots ≥5 min apart show no active series;
  missing or unreachable source state leaves the incident open.
- Specialist transcripts compress into incident-owned, content-addressed
  PostgreSQL artifacts; checkpoints keep references only, and storage failure
  retains the legacy trace rather than losing evidence.
- The canonical SaaS runtime owns the successful durable-job terminal write;
  the queue worker holds the lease. API and Temporal worker share one image
  and verify the same manifest before work.
- Anthropic is the only accepted provider, enforced at Helm render time, at
  startup, and on per-tier router overrides. No surface claims unobserved
  cross-provider fallback.
- Input ceiling = `CONTEXT_WINDOW_TOKENS − CONTEXT_RESERVED_OUTPUT_TOKENS −
  margin`, enforced per iteration by a `pre_model_hook` returning
  `llm_input_messages`, so graph state keeps the full transcript. Trimming cuts
  on turn-group boundaries; an orphaned `tool_result` is a hard rejection.
- Retrieval is measured in two halves that never merge: label-free call shape
  from production (`/agent/metrics → retrieval`) and labeled offline ranking
  (`benchmarks/retrieval_eval.py`). `mrr`, `hit_rate`, `false_positive_rate`
  gate; an unrunnable store reports `skipped`, never a pass.
- Benchmark scenarios are data, never inline Python. `dataset.json` pins a
  SHA-256 per split and for `fixtures.json`, which declares the fault surface
  the workload really exposes; anything a scenario names but the manifest does
  not declare is a load-time `DatasetError`.
- The autonomy threshold is selected, not asserted: the cheapest eligible
  operating point under a recorded `cost_model` and `selection_rule`. Support
  and Wilson floors constrain eligibility but no longer select. Only
  `sre_bench.py` emits `evidence_source=live_benchmark` and only an all-live
  corpus yields a threshold; anything else gets a curve, a null threshold, and
  a stated `autonomy_blocked_reason` reaching operators via `ActReport`.
  `load_calibration_artifact` recomputes everything, so a hand edit fails.
- `SENTINEL_ABLATION_ARM` unset is production; set (even to `full`) it is a
  measurement run. Four arms remove at most one component each, an unknown arm
  raises `AblationError` rather than degrading to the control, and the arm is
  written into the manifest's `runtime` section — one of the four sections the
  A01 fingerprint hashes — so two arms are structurally incomparable. Every
  arm freezes learned-memory writes, the control included, because arms run
  sequentially against one cluster.
- `benchmarks/ablation_eval.py` takes the arm as baseline and the full stack as
  candidate, so paired deltas read "full minus arm". It requires each arm's run
  manifest to re-hash to the fingerprint on that arm's trials, closing the hole
  that `BENCH_CONFIG_FINGERPRINT` is operator-declared. A component is credited
  only when the lower CI bound clears zero; an interval containing zero is
  `NOT_DEMONSTRATED`, annotated when the evidence was too thin to be a null.

## Completed or verified work
- Crash-resumption proven live: a workflow that lost its worker resumed after
  alert clear with one `EXECUTED`, one `REFUSED(incident_resolved)`, one audit,
  no repeated mutation. A CAS blocks late-webhook/reconciler races.
- Langfuse trace `1814f33e…`: 142 observations, zero generic `agent` names, all
  31 generations carrying model and usage metadata.
- Dataset v2: 22 scenarios (12/6/4) over 4 fault targets, all injecting and
  fully restoring in test. v1's `bad_deploy_checkout` probe could never have
  gone healthy; v2's thresholds sit above checkout's organic error ratio.
- Confidence calibration is empirical end to end: on a graded 300-sample
  corpus `false_autonomy_cost=2` selects threshold 0.597 at 80% coverage while
  `=200` selects 0.969 at 20%. Four artifact tamper paths are closed.
- Ablation arms verified by graph shape: control and unset are node- and
  edge-identical; `no_memory` differs only behaviourally; `no_reflector` drops
  ORIENT and its re-investigation loop; `single_agent` keeps the reflector and
  the report writer. Two strawmen were avoided — the planner now reads the
  specialists' findings directly when ORIENT is gone (still wrapped as
  untrusted), and the single investigator writes where the reflector reads.
- Qdrant 1.19.1, PostgreSQL 15.19, Redis 7.4.11, Temporal CLI 1.8.3, Anthropic
  1.7.2 digest-pinned; Temporal SDK 1.32.0.

## Active problem
Nothing in #16-#22 has run against a live cluster. The ablation harness can now
answer whether the specialist split, the reflector and learned memory earn
their cost, but until the four arms are actually run the HolmesGPT comparison
still rests on design description. The same live-run dependency blocks the
confidence calibration artifact.

Deferred, unrelated: digest-pin the Helm Temporal server image.

## Relevant files
- Ablation: `sre_agent/ablation.py` (arms), `benchmarks/ablation_eval.py`
  (harness), `benchmarks/ablation/README.md` (operator runbook),
  `sre_agent/graph_builder.py` (per-arm wiring),
  `sre_agent/config/agent_config.yaml` + `prompts/single_agent_prompt.txt`
  (the single-investigator baseline), and the write gates in `act_phase.py`,
  `supervisor.py`, `agent_runtime.py`.
- Paired statistics: `benchmarks/sre_bench.py` (`BENCH_CANDIDATE_ID`,
  `BENCH_CONFIG_FINGERPRINT`), `benchmarks/statistical_eval.py`.
- Corpus: `benchmarks/datasets/v2/` (+ `fixtures.json`), `scenario_dataset.py`,
  `fault_adapter.py`.
- Confidence: `sre_agent/confidence_calibration.py`,
  `benchmarks/confidence_eval.py`, `benchmarks/confidence/{v1,v2}/`, and the
  loaders behind `DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH` /
  `REMEDIATION_CONFIDENCE_CALIBRATION_PATH`.
- Retrieval: `sre_agent/retrieval_metrics.py`, `skill_store.py`,
  `memory_store.py`, `runbook_index.py`, `benchmarks/retrieval_eval.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration`:
  **1,666 passed** in ~28s. (`test_live_remediation_temporal_workflow` can fail
  on a Temporal test-server port bind when other jobs hold the port; it passes
  run alone.)
- `uv run python benchmarks/retrieval_eval.py --output reports/release-retrieval.json`:
  PASS — 25 probes on v2, mrr/hit_rate/nDCG 1.0, false_positive_rate 0.0,
  memory store `skipped` without `--qdrant-url`.
- `PYTHONPATH=benchmarks .venv/bin/python benchmarks/scenario_dataset.py --version v2`:
  12/6/4 = 22 scenarios, digests matching; `--repin` idempotent.
- `PYTHONPATH=. .venv/bin/python benchmarks/make_release_fixtures.py --check`: exit 0.
- `bash scripts/check_python_quality.sh`: passed.
- Deployment-template gate and secret scan: passed;
  `helm template --set llm.provider=gemini` fails at render time as intended.
- Not re-verified since #16: the exact-revision Docker build and live
  `check_runtime_parity.py`. Last known good — image `82305738…`, revision
  `cf76a94`, fingerprint `13db9184…`, Alembic `e5f6a7b8c9d0` (head).

## Known blockers or risks
- Never stage the untracked secret backup `.env.local-backup-20260910`; the
  `.gitignore` `.env` pattern does not match it. Always use explicit paths in
  `git add`.
- #16–#21 are in the working tree but not deployed or live-verified.
- No real calibration artifact exists and none can be built without a paired
  A05 `sre_bench.py` run against a live cluster; synthetic evidence is refused
  at load time. Until then remediation autonomy stays fail-closed on human
  approval — the intended state, not a gap. The ablation arms have the same
  dependency.
- v2 has never run against a live cluster; the adapter round trip is proven
  only against manifest-derived fakes. Memory scenarios need a checkout pod
  restarted recently enough to sit under 150MB (`leak_kb_per_request` never
  frees its buffer).
- `CONTEXT_WINDOW_TOKENS` is operator-declared; a model with a window under
  200k will under-reserve unless it is set.
- tiktoken fetches its BPE file on first use; an air-gapped image without that
  cache silently degrades to the character heuristic.

## Next bounded task
Deploy #16-#22 and run them under live fire. Specifically: build at the exact
revision, run `check_runtime_parity.py`, then run the four ablation arms back
to back on the v2 holdout under one `BENCH_EXPERIMENT_ID` and `BENCH_PAIR_SEED`
per `benchmarks/ablation/README.md`, capture each arm's run manifest, and
compare with `ablation_eval.py`. The same run produces the first real
confidence calibration artifact. Until then every architectural claim in the
HolmesGPT comparison is measurable but unmeasured.
