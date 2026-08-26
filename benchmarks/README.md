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
`BENCH_ORACLE_RESULTS_PATH`, and `BENCH_ORACLE_COMPLETION_GRACE_SEC`.

The default evidence path is `reports/sre-bench-oracle.jsonl` (git-ignored).
Each record contains the exact probe and its SHA-256, raw timestamped
observations, application status for comparison, and the oracle MTTR.
Aggregate timing keys are explicitly named `oracle_mttr_*`; they are not
historically comparable with the legacy incident-row MTTR.

The current runner still sends a synthetic Alertmanager stimulus; it does not
inject workload faults. A healthy signal that never crosses the failure
boundary is therefore `INVALID_SCENARIO`, never a successful recovery. For a
real MTTR experiment, activate the named fault in the chaos panel immediately
before the run so the oracle observes the failing state. A versioned automated
fault-injection contract belongs in the dataset/scenario work (A03).

### Extending

Add a `ScenarioSpec` to `SCENARIOS` in `sre_bench.py` with its ground truth,
including one aggregate `RecoveryProbe` that returns exactly one scalar. The
probe must define a deterministic healthy comparator/threshold and must not
depend on agent-authored output. Keep scenarios aligned with the failure
taxonomy in `docs/ACT_PHASE_DESIGN.md` §6 so the benchmark tracks real coverage.
