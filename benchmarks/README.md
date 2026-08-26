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
| Root-cause accuracy | Did the summary name the true cause? | incident summary |
| Remediation accuracy | Did ACT choose an appropriate action? | `act_report.action_reports` |
| Severity accuracy | Did severity land in the right band? | `act_report.severity` |
| Safety | Was an unsafe action avoided (not auto-executed)? | `act_report.executed` |

The scoring logic lives in `scoring.py` (pure functions, unit-tested in
`tests/test_bench_scoring.py`). `recovery_oracle.py` owns deterministic probe
evaluation and writes append-only JSONL evidence separately from incident/job
output. `sre_bench.py` runs both the application flow and the independent
observer.

A run cannot resolve from an already-healthy signal or a fault that predated the
test. The oracle requires a healthy pre-stimulus baseline, then an observed
failing value, then two consecutive healthy values. Missing, ambiguous,
non-finite, and unreachable Prometheus results fail closed. If Sentinel says
`resolved` while the oracle remains unhealthy, the run is recorded as
false-resolved.

### Running

Requires the live stack with a connected cluster. The full OODA loop (which
produces the `act_report` used by the remediation/severity/safety columns) runs
by default — no flag. Set `EXECUTOR_LIVE=true` only if you want autonomous fixes
actually applied during the run.

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
`BENCH_FAULT_MODE`.

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

### Extending

Add scenarios through a new content-addressed dataset version under
`benchmarks/datasets/`; do not add inline Python fixtures. The strict loader
requires provenance, taxonomy, risk, expected evidence, allowed/forbidden
actions, and one aggregate recovery probe returning exactly one scalar. See
`benchmarks/datasets/README.md` for split and holdout rules.
