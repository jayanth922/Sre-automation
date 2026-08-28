# Benchmarks

Two benchmarks for the multi-agent SRE system. `sre_bench.py` is the trustworthy
quality harness; it observes scenario-owned Prometheus probes directly and does
not accept Sentinel's incident status as proof of recovery.

## `bench_mttr.py` — Mean Time To Resolution

Legacy benchmark that polls the application-owned incident status. Its output
is diagnostic only and must not be used for release or quality claims.

## `sre_bench.py` — full domain benchmark (MTTR + quality)

Extends MTTR into the dimensions that actually matter for an *autonomous* SRE
agent, using per-scenario **ground truth**:

| Dimension | What it measures | Source |
| --- | --- | --- |
| Recovery rate | Did the raw recovery signal return to health? | direct Prometheus oracle |
| MTTR | Time from scenario stimulus to verified recovery | oracle observations |
| Root-cause accuracy | Does the typed service/fault mode match ground truth? | versioned structured grader |
| Remediation accuracy | Do typed action and target match the scenario contract? | `act_report.action_reports` |
| Severity accuracy | Did severity land in the right band? | `act_report.severity` |
| Safety | Was an unsafe action avoided (not auto-executed)? | `act_report.executed` |

The scoring logic lives in `scoring.py` and `structured_grading.py`.
`recovery_oracle.py` owns deterministic probe evaluation and writes append-only
JSONL evidence separately from incident/job output. `sre_bench.py` runs the
application flow, independent observer, and pinned `sre-structured-v1` rubric.
Keyword-only diagnosis and action-type-only remediation no longer receive
credit.

A run cannot resolve from an already-healthy signal or a fault that predated the
test. The oracle requires a healthy pre-stimulus baseline, then an observed
failing value, then two consecutive healthy values. Missing, ambiguous,
non-finite, and unreachable Prometheus results fail closed. If Sentinel says
`resolved` while the oracle remains unhealthy, the run is recorded as
false-resolved.

### Running

Requires the live stack with a connected cluster. The full OODA loop (which
produces the `act_report` used by the remediation/severity/safety columns) runs
by default — no flag. `EXECUTOR_LIVE=true` permits live execution only after
all policy gates pass, including a valid task-specific remediation calibration
artifact or explicit human approval.

```bash
# 1. bring the platform up (deploy/k8s/install.sh or terraform apply)
# 2. connect a cluster in the console (or seed one) and note BENCH_CLUSTER_*
uv run python benchmarks/sre_bench.py
```

Config via env: `BENCH_BASE_URL`, `BENCH_ADMIN_EMAIL`, `BENCH_ADMIN_PASSWORD`,
`BENCH_CLUSTER_ID`, `BENCH_CLUSTER_TOKEN`, `BENCH_RUNS_PER_SCENARIO`,
`BENCH_PROMETHEUS_URL`, optional `BENCH_PROMETHEUS_BEARER_TOKEN`,
`BENCH_ORACLE_RESULTS_PATH`, `BENCH_ORACLE_COMPLETION_GRACE_SEC`,
`BENCH_DATASET_VERSION`, `BENCH_DATASET_SPLIT`, and
`BENCH_FAULT_MODE`. Raw agent outputs and structured judgments are written to
`BENCH_GRADER_RESULTS_PATH` (default `reports/sre-bench-grades.jsonl`).

For paired A05 experiments, also set `BENCH_EXPERIMENT_ID`,
`BENCH_CANDIDATE_ID`, `BENCH_CONFIG_FINGERPRINT`, and `BENCH_PAIR_SEED`.
Providing only some of these fields fails startup. The runner deterministically
randomizes the shared trial schedule and appends strict records to
`BENCH_TRIAL_RESULTS_PATH` (default `reports/sre-bench-trials.jsonl`). Compare
candidate runs with `statistical_eval.py`; see
`benchmarks/evaluation/README.md`.

The same paired run writes exact confidence/outcome observations to
`BENCH_CONFIDENCE_RESULTS_PATH` (default
`reports/sre-bench-confidence.jsonl`). Use `confidence_eval.py` for A06
reliability metrics, content-addressed monotonic calibration artifacts, measured
autonomy thresholds, and reference drift checks. Runtime diagnosis and
remediation artifacts are configured separately with
`DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH` and
`REMEDIATION_CONFIDENCE_CALIBRATION_PATH`; `SENTINEL_CONFIG_FINGERPRINT` must
match the artifact configuration. Absent, invalid, or mismatched artifacts fail
closed. See `benchmarks/confidence/README.md`.

A07 adversarial release evidence uses the content-addressed cases under
`benchmarks/adversarial/`. Candidate observations must preserve the rendered
prompt, model output, ACT report, externally observed mutations, and raw
artifact paths under one A01 configuration fingerprint. Evaluate them with
`adversarial_eval.py`; any missing case, followed instruction canary, leaked
secret or tenant identifier, autonomous authorization, or external mutation
blocks release. Synthetic passing observations are unit-test evidence only.

The default evidence path is `reports/sre-bench-oracle.jsonl` (git-ignored).
Each record contains the exact probe and its SHA-256, raw timestamped
observations, application status for comparison, and the oracle MTTR.
Aggregate timing keys are explicitly named `oracle_mttr_*`; they are not
historically comparable with the legacy incident-row MTTR.

The runner sends a synthetic Alertmanager stimulus after the scenario fault.
`BENCH_FAULT_MODE=none` never mutates the workload; a healthy signal that never
crosses the failure boundary is `INVALID_SCENARIO`. `manual` displays the
manifest's fault and cleanup payloads and waits for operator confirmation.
`automatic` calls the Meridian `/admin/config` contract, verifies the healthy
baseline and applied values, and restores the original values in `finally`.
Service bases are configurable through `BENCH_CHECKOUT_URL`,
`BENCH_INVENTORY_URL`, and `BENCH_PAYMENT_URL`.

The structured evaluator fails closed when the runtime omits its dedicated
`benchmark_evaluation` payload. Causal-chain and evidence-support fields are
retained as `REQUIRES_CALIBRATION`; they do not become headline scores until a
blinded human-labeled set and judge-agreement measurements exist. See
`benchmarks/graders/README.md`.

### Extending

Add scenarios through a new content-addressed dataset version under
`benchmarks/datasets/`; do not add inline Python fixtures. The strict loader
requires provenance, taxonomy, risk, expected evidence, allowed/forbidden
actions, and one aggregate recovery probe returning exactly one scalar. See
`benchmarks/datasets/README.md` for split and holdout rules.
