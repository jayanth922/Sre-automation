# Project state

## Project objective
Sentinel: a small-scale SRE agent built to production quality — one a senior
SRE would let act on a live cluster. Truthful, tenant-isolated, reproducible
and cost-conscious. Deterministic policy and durable state — not model prose —
control writes, approvals, status transitions and operator-facing claims.

## Current milestone
**Production-quality roadmap** (DECISIONS.md, 2026-10-19), in order:
1. **Safe actions**: explicit, precondition-checked, idempotent remediation;
   approval bound to the plan and the observed state. ← next
2. **Spend and noise rails**: per-incident and daily $ caps, kill switch,
   follow-on alerts grouped into the open incident.
3. **$0 full-path dry run** in CI (stubbed/replayed model): inject → alert →
   approval → rollback → verify.
4. Act 1 rehearsal 3 (paid, ~$1.50–2.00), then the live demo.
5. Error-budget burn-rate paging; the agent's own SLOs and dashboard.
6. Reproducible delivery: CI-built images, no `docker cp`.
7. Judge calibration (`causal_chain`/`evidence_support`); replay evals gate CI.
Standing rule: batch every open fix, verify offline, then spend.

## Current architecture and invariants
- Layout: `src/` (agent, API), `apps/dashboard/`, `services/` (MCP, backend),
  `evals/benchmarks/`, `infra/`, `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole tenant, policy,
  idempotency and audit boundary. `policy_gate.decide` verdicts cannot be
  appealed; bans are judged from measured state, never the planner's risk.
- Recovery is the scenario's Prometheus probe, never incident status.
- **Runbooks are the only fixes** (`RUNBOOK_ONLY_REMEDIATION`, default on):
  a plan passes only if one reviewed runbook branch names every mutating
  action's executor tool; otherwise `runbook_gap` escalation.
- Slack is the action surface; the console observes and configures.
- Fault adapters (`evals/benchmarks/fault_adapter.py`): `/admin/config`
  faults are in-memory; `k8s_deployment_env_v1` rolls out a real revision
  that only a rollback clears.
- Bench: alert posts at the probe's first failing reading
  (`BENCH_FAULT_VISIBLE_SEC`); `awaiting_approval` ends a trial unless
  `BENCH_AWAIT_HUMAN_APPROVAL=1` (demo mode, own arm and fingerprint); every
  campaign ends with a quiesce (`BENCH_QUIESCE_SEC`=420).
- Any new artifact field needs a round-trip test through the real producer
  and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios) and two paid campaigns, both NOT_DEMONSTRATED.
  Do not quote #70's MTTR.
- PRs #57–#60 merged. Review set: 25 cases in `reports/judge-calibration/`;
  never label `superseded-2026-10-01/`.
- Branch `act1-bad-config-deploy` (unpushed): 11fa8c1 deploy adapter +
  `checkout_bad_config_deploy` + quiesce; 69997b7 alert-on-visible; a9d3ab5
  human-approval wait.
- Act 1 rehearsals 2026-10-18, both UNRESOLVED on harness bugs now fixed:
  #1 ($1.19) alert before the probe failed → Branch A closed it; #2 ($1.37)
  correct Branch E rollback proposal, trial ended at `awaiting_approval`.

## Active problem
Gaps found 2026-10-19 (targeted search, not a full audit):
- `executor.build_command` turns `rollback` into `kubectl rollout undo`
  (relative). It ignores the plan's `to_revision`, so the approver sees a
  target that is not what runs, and undo toggles: a retry, a double execution
  or an earlier restart lands on the faulty revision.
- No check found that the cluster still matches what the approver saw.
  Approvals are refused on resolved incidents (`approval_flow.py:171`,
  untested live) and expire after 30 min.
- No LLM spend cap found in `src/sre_agent`; only stopping the API halts spend.
- No run grades better than `INCOMPLETE`: `_semantic_criterion` has no judge.

## Relevant files
- Step 1: `src/sre_agent/{executor,mutation_gateway,approval_flow,policy_gate}.py`,
  the planner's rollback parameters,
  `examples/meridian/runbooks/high-error-rate.md` (Branch E).
- Bench: `evals/benchmarks/{sre_bench,fault_adapter}.py`,
  `evals/benchmarks/datasets/v3/`, `docs/DEMO.md`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2746 passed,
  6 skipped (2026-10-18).
- Codespace `cuddly-winner-659v67gv695hrxjw` idle-shuts down; after a restart
  run `scripts/dev/codespace_boot.sh` and wait
  ≥5 min before injecting. No git credentials there: push via a git bundle
  from the Mac. Its checkout has a9d3ab5's bench and DEMO.md, sha256-verified.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api dashboard`, then `up -d --no-build
  --no-deps --force-recreate`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price first. ≈$16.60 spent
  through 2026-10-18. Calibration campaign (~$40+) not authorized.
- `sre-agent-api` and `sre-temporal-worker` are stopped; with the API up, any
  alert opens a paid investigation.
- FAST (haiku-4-5) untested in a paid run; `sonnet-4-5` retires 2026-11-30.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged restore files alone.

## Next bounded task
**Roadmap step 1: safe rollback** (free, offline). Acceptance:
- A rollback names its target by an identity that survives revision
  renumbering (the ReplicaSet's pod-template hash), plus the template it
  expects to replace, both captured from the investigation's evidence.
- The approval request shows both, and the approval binds to them.
- At execution the gateway re-reads the Deployment: already on the target →
  success with no write; on the expected template → roll back to the target
  explicitly; anything else → refuse and escalate as state drift.
- Tests: double approval, retry after success, restart before rollback, drift
  between approval and execution, displayed command equals executed command.
Then step 2. Push only on the user's word. User: hand-label the 25 judge
cases (two blind passes ≥48h apart).
