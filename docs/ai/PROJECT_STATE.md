# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** of the new Act 1 (`checkout_bad_config_deploy`): two
   paid trials UNRESOLVED, each from a harness fault now fixed offline; a
   third awaits approval.
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
- The bench posts a trial's alert only at the probe's first failing reading
  (`_await_fault_visible`, `BENCH_FAULT_VISIBLE_SEC`=360); controls, unarmed
  probes and bad baselines post at injection.
- `awaiting_approval` ends a trial unless `BENCH_AWAIT_HUMAN_APPROVAL=1`
  (demo mode: waits for the Slack decision; own arm and fingerprint).
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
- 2026-10-17 `payment_provider_outage` rehearsal: UNRESOLVED, $2.44 (ConfigMap
  fault no allowed action restores; $1.00 cascade spend). Replaced as Act 1.
- Branch `act1-bad-config-deploy` (unpushed): 11fa8c1 deploy adapter +
  `checkout_bad_config_deploy` (v3 dev, rollback only) + quiesce; 69997b7
  alert-on-visible fix.
- **Act 1 rehearsal 1 (2026-10-18, $1.19): UNRESOLVED.** Alert posted at
  injection; Step 1 probe read 0.146 < 0.45, so Branch A closed it. Fixed by
  `_await_fault_visible`.
- **Rehearsal 2 (2026-10-18, $1.37): UNRESOLVED.** Agent measured 0.62,
  excluded Branches A–D with evidence, proposed Branch E `rollback` →
  `requires_approval`; the bench ended the trial at `awaiting_approval` before
  anyone approved. Fixed by `BENCH_AWAIT_HUMAN_APPROVAL`.
- Free mechanics test: inject → alert 3m40s → `rollout undo` → recovered 1m40s.

## Active problem
- Untested: the Slack approval click, rollback execution and probe
  verification. Approvals expire after 30 min (`APPROVAL_TTL_MINUTES`). The
  plan's `to_revision` is ignored: the executor always runs `rollout undo` —
  right for Act 1, but the approver sees a revision number that is not what
  runs.
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
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2746 passed,
  6 skipped (2026-10-18).
- Notion publish: `publish_meridian_runbooks.py --cluster-id
  "$BENCH_CLUSTER_ID"` on the Codespace host, `DATABASE_URL` at
  `sre-postgres`'s bridge IP; dry run unless `--confirm`.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api dashboard`, then `up -d --no-build
  --no-deps --force-recreate`.
- Act 1 trial: the `docs/DEMO.md` command with `/home/vscode/bench.env`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price first. ≈$16.60 spent
  through 2026-10-18. Calibration campaign (~$40+) not authorized.
- `sre-agent-api` and `sre-temporal-worker` are stopped; with the API up, any
  alert opens a paid investigation. Only stopping the API halts one.
- FAST (haiku-4-5) untested in a paid run; `sonnet-4-5` retires 2026-11-30.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged restore files alone.
- `watch_meridian_deploy.sh` is opt-in; if enabled it `reset --hard`s to
  GitHub master (checkout ~84% 5xx).

## Next bounded task
1. With approval: start the API and worker (`docker start`), run Act 1
   rehearsal 3 with `BENCH_AWAIT_HUMAN_APPROVAL=1` and a human Slack approval
   (approve the rollback, reject a restart), watch spend past exit; stop both
   containers after.
2. Push only on the user's word.
3. User: hand-label the 25 cases (two blind passes ≥48h apart).
4. Free: judge harness + `CALIBRATION_DESIGN.md` §5 on a stub judge.
