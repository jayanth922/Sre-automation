# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
**Calibrated judge for `causal_chain` / `evidence_support` — free phase.**
E2E runs through Slack are done (Runs 1–9, 2026-09-28/30; all found defects
fixed). Standing rule: batch every open fix, verify offline, then spend.

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
- **Runbooks are the only fixes (b5f774d).** `runbook_authority` passes a plan
  only if one branch of a reviewed runbook listing the alert names every
  mutating action's executor tool; otherwise the plan becomes an escalation
  carrying `runbook_gap`. `RB-AUTO`/Auto-generated pages are drafts.
- Slack is the action surface; the console observes and configures. No
  env setup for users — everything configurable in Settings.
- Span completeness ≠ outcome completeness: a correct no-action run has no
  act-path spans and may carry no MTTR. Any new artifact field needs a
  round-trip test through the real producer and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios, oracles, structured grading, paired stats,
  ablations, content-addressed release gate); two paid campaigns, both
  NOT_DEMONSTRATED (2 valid pairs each). Do not quote #70's MTTR.
- E2E Runs 1–9 fixes (Sre-automation through `196eb31`, incl. `439a7e7`
  severity split, `db85a32` reflector, `196eb31` output ceiling; meridian
  `7aa8d68`, `a23e9b4`) — details in git history. Negative control
  `payment_subthreshold_charge_errors`: NO_ACTION_CORRECT every run (3–9);
  other six deterministic criteria PASS since Run 7.
- Unpaired evidence: smoke runs write `reports/sre-bench-unpaired-{trials,
  root-traces}.jsonl` (`BENCH_RECORD_UNPAIRED`, default on), never mixed
  into the paired file. Run traces persist in `platform_reports_data`.

## Active problem
Both criteria are `REQUIRES_CALIBRATION` because
`structured_grading._semantic_criterion` is a shape check with no judge, so
no run can be better than `INCOMPLETE`. Design:
`evals/benchmarks/graders/CALIBRATION_DESIGN.md` (rubric judged against the
agent's own tool transcripts, never scenario ground truth — `diagnosis` owns
that; labels in the existing `grader_calibration` contract).
- `run-trace/*.jsonl` holds no payloads — only a `root_trace_id` join key.
  Claims live in grader records; tool returns in Postgres `evidence_artifacts`.
- Branch `judge-calibration` (worktree `/workspaces/sre-judge-calibration`):
  `calibration_cases` no longer aborts a file on identical *empty* outputs
  (that rejected all of `reports/sre-bench-grades.jsonl`), takes several
  grader files with cross-file dedupe, and attaches sha-verified transcripts;
  new `benchmarks.transcript_store` builds the store from a `psql` export.
- Built set (2026-10-01): `reports/judge-calibration/` — 25 blinded cases
  (6 negative-control), 114 transcripts attached, 0 missing; 9 cases have an
  empty evidence list (causal_chain only). Key and private map are 0600.

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
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2621 passed, 6 skipped (2026-10-01, `judge-calibration`).
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
Before the rehearsal: dump the live Notion corpus (`dump_notion_runbook_corpus.py`)
and confirm the four Meridian pages match `examples/meridian/runbooks/` — the
live High Latency page is stale and the PaymentProviderDown page is unverified.
Hand-label the 25 cases in `reports/judge-calibration/review.jsonl` (two
blind passes ≥48h apart, distinct `labeler_id`s), adjudicate, freeze. Then,
still free: perturbation generator, deterministic `locatable` pre-check, judge
harness verified against a stub judge. Only then price one judge pass and the
paid calibration campaign (last estimate ~$40+) and ask for approval.
