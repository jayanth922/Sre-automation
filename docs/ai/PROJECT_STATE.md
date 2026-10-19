# Project state

## Project objective
Sentinel: a small-scale SRE agent built to production quality — one a senior
SRE would let act on a live cluster. Truthful, tenant-isolated, reproducible
and cost-conscious. Deterministic policy and durable state — not model prose —
control writes, approvals, status transitions and operator-facing claims.

## Current milestone
**Production-quality roadmap** (DECISIONS.md, 2026-10-19), in order:
1. **Safe actions**: explicit, precondition-checked, idempotent remediation;
   approval bound to the plan and the observed state. Rollback done (branch
   `safe-rollback`, unpushed).
2. **Spend and noise rails**: per-incident and daily $ caps, kill switch,
   follow-on alerts grouped into the open incident. ← next
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
- **Rollbacks are bound** (`rollback_binding.py`): a graph node after
  `severity_telemetry` reads the executor's `get_rollout_state` and writes the
  target and expected pod-template hashes into the plan, so the approval hash
  covers them. Unbound → BLOCKED. Before the idempotency claim the gateway
  re-reads: on target → no-write EXECUTED; off the expected template →
  `state_drift`, manual review. The edge re-reads and CAS-patches. Target =
  newest differing revision that predates the current software.
- Slack is the action surface; the console observes and configures.
- Fault adapters (`evals/benchmarks/fault_adapter.py`): `/admin/config`
  faults are in-memory; `k8s_deployment_env_v1` rolls out a real revision
  that only a rollback clears.
- Bench: alert posts at the probe's first failing reading
  (`BENCH_FAULT_VISIBLE_SEC`); `awaiting_approval` ends a trial unless
  `BENCH_AWAIT_HUMAN_APPROVAL=1` (demo mode, own arm and fingerprint); every
  campaign ends with a quiesce (`BENCH_QUIESCE_SEC`=420).
- New artifact fields need a producer→consumer round-trip test.

## Completed or verified work
- v2 benchmark (22 scenarios) and two paid campaigns, both NOT_DEMONSTRATED.
  Do not quote #70's MTTR.
- PRs #57–#60 merged. Review set: 25 cases in `reports/judge-calibration/`;
  never label `superseded-2026-10-01/`.
- Unpushed: `act1-bad-config-deploy` (deploy adapter, alert-on-visible,
  human-approval wait), then `safe-rollback` on top of it.
- Act 1 rehearsals 2026-10-18 ($1.19, $1.37), both UNRESOLVED on harness bugs
  now fixed; #2 reached a correct Branch E rollback proposal.

## Active problem
Gaps found 2026-10-19 (targeted search, not a full audit):
- No LLM spend cap found in `src/sre_agent`; only stopping the API halts spend.
- Other mutations (restart, scale, env/limits patch) have no observed-state
  precondition yet; their displayed inverse is still a relative undo.
- No run grades better than `INCOMPLETE`: `_semantic_criterion` has no judge.

## Relevant files
- Rollback: `src/sre_agent/{rollback_binding,executor,mutation_gateway}.py`,
  `services/edge_mcp_servers/mcp_servers/executor_real/{rollout,server}.py`,
  `tests/test_{safe_rollback,executor_rollout}.py`.
- Bench: `evals/benchmarks/{sre_bench,fault_adapter}.py`,
  `evals/benchmarks/datasets/v3/`, `docs/DEMO.md`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2795 passed,
  6 skipped (2026-10-19, branch `safe-rollback`).
- Codespace `cuddly-winner-659v67gv695hrxjw` idle-shuts down; after a restart
  run `scripts/dev/codespace_boot.sh`, wait ≥5 min before injecting. Push via
  a git bundle from the Mac. Its checkout has a9d3ab5's bench.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api dashboard`, then `up -d --no-build
  --no-deps --force-recreate`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price first. ≈$16.60 spent
  through 2026-10-18. Calibration campaign (~$40+) not authorized.
- `sre-agent-api` and `sre-temporal-worker` are stopped; with the API up, any
  alert opens a paid investigation.
- FAST (haiku-4-5) untested in a paid run; `sonnet-4-5` retires 2026-11-30.
- The executor edge image must be rebuilt and redeployed (new `rollout.py`,
  `get_rollout_state`, kubectl removed); until then every rollback is blocked
  before approval. Untested against a real apiserver.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged restore files alone.

## Next bounded task
**Roadmap step 2: spend and noise rails** (free, offline). Start by finding
the LLM call choke point(s). Acceptance:
- Every model call is metered to its incident and the UTC day; a call that
  would cross the per-incident or daily cap is refused, and the incident
  escalates showing the spend.
- A kill switch stops new investigations and model calls without stopping
  the API.
- An alert for a service with an open incident joins it instead of opening a
  new paid investigation (the cascade-alert spend).
- Tests: cap hit mid-investigation, daily cap, kill switch, grouped alert.
Push only on the user's word. User: hand-label the 25 judge cases (two blind
passes ≥48h apart).
