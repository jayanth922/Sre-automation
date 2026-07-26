# Benchmarks

Two benchmarks for the multi-agent SRE system. Both fire synthetic Alertmanager
webhooks at the running platform and measure the pipeline end to end — no code
changes to the app are required.

## `bench_mttr.py` — Mean Time To Resolution

Fires each scenario `RUNS_PER_SCENARIO` times, polls until the incident is
RESOLVED, and computes MTTR from the stored timestamps. Reports mean/median/p95
against published baselines.

## `sre_bench.py` — full domain benchmark (MTTR + quality)

Extends MTTR into the dimensions that actually matter for an *autonomous* SRE
agent, using per-scenario **ground truth**:

| Dimension | What it measures | Source |
| --- | --- | --- |
| Resolution rate | Did the incident resolve? | incident status |
| MTTR | Time to resolve | timestamps |
| Root-cause accuracy | Did the summary name the true cause? | incident summary |
| Remediation accuracy | Did ACT choose an appropriate action? | `act_report.action_reports` |
| Severity accuracy | Did severity land in the right band? | `act_report.severity` |
| Safety | Was an unsafe action avoided (not auto-executed)? | `act_report.executed` |

The scoring logic lives in `scoring.py` (pure functions, unit-tested in
`tests/test_bench_scoring.py`) so it can be validated without a running platform.
`sre_bench.py` is the runner that fetches data over HTTP and applies it.

### Running

Requires the live stack, and — for the remediation/severity/safety columns — the
ACT phase enabled:

```bash
./main_start.sh                 # platform + edge + Target_Client
export ACT_PHASE_ENABLED=true   # so the act_report is produced
# optional: export EXECUTOR_LIVE=true   # to actually apply autonomous fixes
uv run python benchmarks/sre_bench.py
```

Config via env: `BENCH_BASE_URL`, `BENCH_ADMIN_EMAIL`, `BENCH_ADMIN_PASSWORD`,
`BENCH_CLUSTER_ID`, `BENCH_CLUSTER_TOKEN`, `BENCH_RUNS_PER_SCENARIO`.

### Extending

Add a `ScenarioSpec` to `SCENARIOS` in `sre_bench.py` with its ground truth
(expected service, root-cause keywords, expected action types, severity band,
and any unsafe actions). Keep scenarios aligned with the failure taxonomy in
`docs/ACT_PHASE_DESIGN.md` §6 so the benchmark tracks real coverage.
