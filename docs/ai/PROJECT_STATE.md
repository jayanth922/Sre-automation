# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** of the new Act 1 (`checkout_bad_config_deploy`): code,
   docs and Notion ready; one paid trial awaiting approval.
2. **Calibrated judge for `causal_chain` / `evidence_support`** — free phase.
Standing rule: batch every open fix, verify offline, then spend.

## Current architecture and invariants
- Layout: `src/` (agent, API), `apps/dashboard/`, `services/` (MCP, backend),
  `evals/benchmarks/`, `infra/`, `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole tenant, policy,
  idempotency and audit boundary. `policy_gate.decide` verdicts cannot be
  appealed; bans are judged from measured state, never the planner's risk.
- Severity: reported `measured_severity` vs gate-read `policy_severity`.
- Recovery is the scenario's Prometheus probe, never incident status.
- **Runbooks are the only fixes** (`RUNBOOK_ONLY_REMEDIATION`, default on):
  a plan passes only if one reviewed runbook branch names every mutating
  action's executor tool; otherwise `runbook_gap` escalation. Each Notion
  runbook's `Alert Name` property must list every alert its body covers.
- Slack is the action surface; the console observes and configures.
- Routed clusters have no static model; tiers in
  `model_router._ANTHROPIC_TIER_DEFAULTS`.
- Fault adapters (`evals/benchmarks/fault_adapter.py`): `/admin/config`
  faults are in-memory (a restart clears them); `k8s_deployment_env_v1` rolls
  out a real revision that only a rollback clears. `FaultAdapterRouter`
  dispatches by `fault.adapter`. See DECISIONS.md.
- The bench quiesces after every campaign (`_quiesce_campaign`,
  `BENCH_QUIESCE_SEC`=420): resolves incidents it opened, cancels their jobs,
  waits for zero pending/firing alerts.
- Any new artifact field needs a round-trip test through the real producer
  and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios) and two paid campaigns, both NOT_DEMONSTRATED.
  Do not quote #70's MTTR.
- PRs #57–#60 merged (runbook-only remediation, routing, watcher opt-in).
- Review set: 25 cases in `reports/judge-calibration/`; never label
  `superseded-2026-10-01/`.
- 2026-10-17 rehearsal (`payment_provider_outage`): UNRESOLVED, $2.44 — its
  fault lives in a ConfigMap no allowed action restores, and a cascade
  incident spent $1.00 after exit. Replaced as Act 1.
- **2026-10-18, uncommitted:** deploy adapter + `checkout_bad_config_deploy`
  (v3 dev, rollback only, `taxonomy.fault_mode = bad_deploy`), quiesce guard,
  `docs/DEMO.md` Act 1, datasets README, runbook baseline (healthy checkout
  ~0.00, up to 0.35 from inventory holds; was "standing 0.35").
- **Free live mechanics test (2026-10-18, Codespace):** inject 04:19:20Z →
  `CheckoutHighErrorRate` firing 04:23:00Z (ratio 0.53+; no
  `PaymentFailureSpike`, max per-reason rate ≈0.40 projected) → `rollout undo`
  04:24:08Z → probe < 0.45 twice by 04:25:48Z → alert cleared 04:29:11Z.
  Adapter cleanup verified, no new revision. Healthy revision is now 15.
- Notion `High Error Rate` page republished; live body equals the draft;
  audit 22/22.

## Active problem
- Act 1 rehearsal not yet run. The agent must find the onset revision
  (`get_deployment_spec` revision/env/last rollout, `list_events`) or Branch E
  step 3 restarts instead — graded "not allowed", and a restart before undo
  makes `rollout undo` land on the faulty revision.
- **No run can grade better than `INCOMPLETE`**: `_semantic_criterion` is a
  shape check with no judge.

## Relevant files
- `evals/benchmarks/{sre_bench,fault_adapter,scenario_dataset}.py`,
  `evals/benchmarks/datasets/v3/`, `tests/test_{fault_adapter,bench_quiesce}.py`.
- `examples/meridian/runbooks/high-error-rate.md`, `docs/DEMO.md`.
- `src/sre_agent/{runbook_authority,approval_flow,job_store}.py`.

## Verification commands and latest results
- Codespace `cuddly-winner-659v67gv695hrxjw`; after a restart run
  `scripts/dev/codespace_boot.sh` (k3s; it does not start the API/worker).
  No git credentials there: push via a git bundle from the Mac. Its checkout
  has the 2026-10-18 files copied in, sha256-verified.
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2735 passed,
  6 skipped (2026-10-18).
- Notion publish/verify from the Codespace host: `publish_meridian_runbooks.py
  --cluster-id "$BENCH_CLUSTER_ID"` with `DATABASE_URL` pointed at
  `sre-postgres`'s bridge IP (`.env` + `bench.env` sourced); dry run unless
  `--confirm`; `--drafts <dir>` limits pages.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api dashboard`, then `up -d --no-build
  --no-deps --force-recreate`. API health: `/ping` on 8080.
- Act 1 trial: the `docs/DEMO.md` command, secrets from
  `/home/vscode/bench.env`. SIGINT does not stop a trial early.

## Known blockers or risks
- Budget: no paid run without explicit approval; price first. ≈$14.04 spent
  through 2026-10-17. Calibration campaign (~$40+) not authorized.
- `sre-agent-api` and `sre-temporal-worker` are stopped; with the API up, any
  alert opens a paid investigation. Only stopping the API halts one.
- FAST (haiku-4-5) untested in a paid run; `sonnet-4-5` retires 2026-11-30.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged restore files alone. Wait ≥5 min after cluster
  changes before injecting.
- `watch_meridian_deploy.sh` is opt-in; if enabled it `reset --hard`s to
  GitHub master (checkout ~84% 5xx).

## Next bounded task
1. With approval: start the API and worker, re-check the Slack approver
   mapping, run one Act 1 trial with a human Slack approval, watch spend past
   exit with a heartbeat monitor.
2. Commit the 2026-10-18 work; push only on the user's word.
3. User: hand-label the 25 cases (two blind passes ≥48h apart).
4. Free: judge harness + `CALIBRATION_DESIGN.md` §5 on a stub judge.
