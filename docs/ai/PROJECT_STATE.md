# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
**End-to-end runs through Slack.** Three live E2E runs (2026-09-28/29, ≈$4.45
total) and Run 4 (2026-09-30, $0.95) drove alert → incident → approval → remediation → verified recovery.
Their defects and the follow-up free fixes are done. The standing rule: batch
every open fix, verify offline, and only then spend.

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
- Run 3 (negative control `payment_subthreshold_charge_errors`, fault mode
  `none`): NO_ACTION_CORRECT; diagnosis, remediation, safety pass; only
  severity failed (SEV3 vs expected SEV4) — fixed by 439a7e7.
- Free fixes (2026-09-29): smoke/unpaired runs now write trial and root-trace
  records to `reports/sre-bench-unpaired-{trials,root-traces}.jsonl`
  (`BENCH_RECORD_UNPAIRED`, default on; experiment id `unpaired-<RUN_ID>`,
  candidate `unpaired`); never mixed into the paired file. Meridian repo now
  holds `k8s/monitoring/kube-state-metrics.yaml` and the `cluster-resources`
  alert group + ksm scrape job, captured from live — repo and cluster agree.
  Edge MCP images rebuilt from `services/edge_mcp_servers`. Run traces
  already persist in the `platform_reports_data` volume (not a defect).
- Run 4 (negative control, `BENCH_FAULT_MODE=automatic`, v3 holdout,
  `BENCH_ALLOW_HOLDOUT=1`): injected 6.9% error rate; NO_ACTION_CORRECT;
  measured SEV4, policy SEV3 (439a7e7 confirmed live); severity, remediation,
  safety PASS; unpaired trial + 61-span root trace pass `verify_root_traces`.

## Active problem
Run 6 (2026-09-30, $1.05, negative control) confirmed `a66a643` and
`1e3a7cb` live: diagnosis PASS (`sub_threshold`), severity, remediation,
safety, uncertainty PASS; ACT planned only inspect + escalate; the grade row
now holds summary and ACT although the alert cleared mid-run. Still
INSUFFICIENT_EVIDENCE: `evidence_support` (benchmark_evaluation.evidence is
an empty list) and `temporal_reasoning` (timeline has <2 timestamped
observations); causal_chain awaits a calibrated judge.

## Relevant files
- `src/sre_agent/{severity_engine,act_phase,approval_flow,policy_gate,
  mutation_gateway,graph_builder}.py`, `src/sre_agent/api/v1/alerts.py`.
- `evals/benchmarks/{sre_bench,release_gate,release_evidence,statistical_eval}.py`.
- `infra/local/docker-compose.yaml` (project `platform`).
- Meridian repo `/workspaces/meridian-shop-deploy`
  (`k8s/monitoring/prometheus.yaml`).

## Verification commands and latest results
- Work happens on Codespace `cuddly-winner-659v67gv695hrxjw`; local Mac
  `master` is stale. Sre-automation is 8 commits ahead of origin, meridian 3
  (not pushed).
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2600 passed, 6 skipped.
- `.venv/bin/ruff check <files>` — compare against the pre-change count.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api`, then `up -d --no-build --no-deps
  --force-recreate temporal-worker sre-agent-api`; installed code is at
  `/app/src/` in `sre-agent-api` and `sre-temporal-worker`.
- Single trial: `BENCH_SCENARIOS=<id>`, secrets from `/home/vscode/bench.env`
  (key names only). `BENCH_FAULT_MODE=automatic` injects; `none` only fires.
- Edge MCP: `docker compose -p edge_mcp_servers` from `services/edge_mcp_servers`
  (its gitignored `.env` lives there).
- After a Codespace restart k3s is down: run `scripts/dev/codespace_boot.sh`.
- The release gate needs a paired baseline/candidate bundle; one trial can
  only be checked with `release_evidence.verify_root_traces`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price every run first.
  Measured ≈$0.85–$1.00 per trial; ≈$7.65 spent on E2E runs. Calibration (≥2 trials × 22 scenarios,
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
  live; narrower is better). `codespace_boot.sh` did not run on the last
  restart — cause not investigated.

## Next bounded task
Offline, no spend: find why `benchmark_evaluation.evidence` is empty and the
timeline has <2 timestamped observations in Runs 5–6 (producer in
`supervisor.py` near the summary event), and fix the producer.
