# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
**End-to-end runs through Slack — done.** Runs 1–9 (2026-09-28/30) drove
alert → incident → approval → remediation → verified recovery, and the
negative control; all found defects are fixed. Standing rule: batch every
open fix, verify offline, and only then spend.

## Current architecture and invariants
- Layout: `src/` (agent, API), `apps/dashboard/`, `services/` (MCP, backend),
  `evals/benchmarks/`, `infra/`, `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole tenant, policy,
  idempotency and audit boundary. `policy_gate.decide` verdicts cannot be
  appealed; bans are judged from measured state, never the planner's risk.
- **Severity has two values (439a7e7).** `measured_severity` / `ActReport.severity`
  is what is reported; the uncalibrated-confidence round-up lives only in
  `policy_severity`, which is what `gate_context` and the policy gate read.
  Unknown telemetry still raises the reported value. See DECISIONS.md.
- Recovery is the scenario's Prometheus probe, never incident status.
- Slack is the action surface; the console observes and configures. No
  env setup for users — everything configurable in Settings.
- Span completeness ≠ outcome completeness: a correct no-action run has no
  act-path spans and may carry no MTTR. Any new artifact field needs a
  round-trip test through the real producer and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios, oracles, structured grading, paired stats,
  ablations, content-addressed release gate); two paid campaigns, both
  NOT_DEMONSTRATED (2 valid pairs each). Do not quote #70's MTTR.
- E2E Run 1–3 fixes (Sre-automation `757bdc2`, `6adaf3c`, `bfa7043`,
  `439a7e7`; meridian `7aa8d68`, `a23e9b4`): alert cleared by the crash it
  reports no longer closes the incident (workload crash probe); resolution and
  executor-output defects; payment restart no longer clears a provider
  outage; inventory alert ignores organic 404s; gateway metric label bounded.
- Unpaired evidence: smoke runs write `reports/sre-bench-unpaired-{trials,
  root-traces}.jsonl` (`BENCH_RECORD_UNPAIRED`, default on), never mixed
  into the paired file. Meridian repo and cluster agree on
  kube-state-metrics and the `cluster-resources` alerts. Run traces persist
  in the `platform_reports_data` volume.
- Negative control `payment_subthreshold_charge_errors` (Runs 3–9):
  NO_ACTION_CORRECT every run; measured SEV4, policy SEV3 (`439a7e7`);
  reflector and output-ceiling fixes (`db85a32`, `196eb31`) confirmed live.

## Active problem
None open on the E2E path. Run 9 (2026-09-30, $1.24, negative control,
automatic, v3 holdout) confirmed `196eb31` live: NO_ACTION_CORRECT, 63-span
trace complete, no lane cut off (no `output_truncated`, no "model turn
stopped on" warnings). Peaks: planning 3016, specialist 3876, reflector
5818/5227 — nothing reached 4096, so the 8192 headroom was not exercised
live; the cut-off-after-tool-calls path is covered offline for both stop
spellings (`length` via LiteLLM, `max_tokens`) in
`tests/test_specialist_output_ceiling.py`. Criterion states unchanged since
Run 7: diagnosis, severity, remediation, safety, uncertainty,
temporal_reasoning PASS; causal_chain and evidence_support
REQUIRES_CALIBRATION (no calibrated judge). Further negative-control repeats
add nothing; stop spending on them.

## Relevant files
- `src/sre_agent/{severity_engine,act_phase,approval_flow,policy_gate,
  mutation_gateway,graph_builder}.py`, `src/sre_agent/api/v1/alerts.py`.
- `evals/benchmarks/{sre_bench,release_gate,release_evidence,statistical_eval}.py`.
- `infra/local/docker-compose.yaml` (project `platform`).
- Meridian repo `/workspaces/meridian-shop-deploy`
  (`k8s/monitoring/prometheus.yaml`).

## Verification commands and latest results
- Work happens on Codespace `cuddly-winner-659v67gv695hrxjw`; local Mac
  `master` is stale. Sre-automation is pushed through `196eb31`; meridian
  is 3 commits ahead (not pushed).
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2610 passed, 6 skipped.
- `.venv/bin/ruff check <files>` — compare against the pre-change count.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api`, then `up -d --no-build --no-deps
  --force-recreate temporal-worker sre-agent-api`; installed code is at
  `/app/src/` in `sre-agent-api` and `sre-temporal-worker`.
- Single trial: `BENCH_SCENARIOS=<id>` and `BENCH_RUNS_PER_SCENARIO=1`
  (default is 3), `BENCH_DATASET_VERSION=v3 BENCH_DATASET_SPLIT=holdout`;
  secrets from `/home/vscode/bench.env` (key names only). `BENCH_FAULT_MODE=automatic` injects; `none` only fires.
- Edge MCP: `docker compose -p edge_mcp_servers` from `services/edge_mcp_servers`
  (its gitignored `.env` lives there).
- After a Codespace restart k3s is down: run `scripts/dev/codespace_boot.sh`.
- The release gate needs a paired baseline/candidate bundle; one trial can
  only be checked with `release_evidence.verify_root_traces`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price every run first.
  Measured ≈$0.85–$1.00 per trial; ≈$11.60 spent on E2E runs (through Run 9). Calibration (≥2 trials × 22 scenarios,
  ~$40+) is not authorized.
- Structured grading cannot return PASS until `causal_chain` has a calibrated
  judge; no calibration artifact exists, so every run rounds policy severity up.
- 3 of 22 scenarios need deploy/rollback evidence no injection creates.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged `checkout-service/app.py`, `.last_deployed_sha` and
  `.local-baseline-restore.md` alone. Wait ≥5 min after cluster changes
  before injecting.
- kube-state-metrics' ClusterRole can list/watch secrets (kept faithful to
  live; narrower is better). On the 2026-09-30 restart `codespace_boot.sh`
  found k3s already running (something else starts it) — not investigated.

## Next bounded task
Milestone: a calibrated judge for `causal_chain` and `evidence_support`.
Free first: rubric, a hand-labelled reference set from Run 3–9 traces
(`reports/run-trace/` in `platform_reports_data`), judge harness and an
agreement metric verified offline. Only then price the paid calibration
campaign (last estimate ~$40+) and ask for approval.
