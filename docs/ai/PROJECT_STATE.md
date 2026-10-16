# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** (`docs/DEMO.md`, Act 1 `payment_provider_outage`) —
   blocked on credentials, see below.
2. **Calibrated judge for `causal_chain` / `evidence_support`** — free phase.
Standing rule: batch every open fix, verify offline, then spend.

## Current architecture and invariants
- Layout: `src/` (agent, API), `apps/dashboard/`, `services/` (MCP, backend),
  `evals/benchmarks/`, `infra/`, `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole tenant, policy,
  idempotency and audit boundary. `policy_gate.decide` verdicts cannot be
  appealed; bans are judged from measured state, never the planner's risk.
- Severity has two values (439a7e7): reported `measured_severity` vs
  `policy_severity` (uncalibrated round-up) read by the gate. See DECISIONS.md.
- Recovery is the scenario's Prometheus probe, never incident status.
- **Runbooks are the only fixes (b5f774d, `RUNBOOK_ONLY_REMEDIATION`,
  default on).** `runbook_authority` passes a plan only if one branch of a
  reviewed runbook listing the alert names every mutating action's executor
  tool; otherwise it becomes an escalation carrying `runbook_gap`, and with no
  applicable runbook the model is never asked. `RB-AUTO`/Auto-generated pages
  are drafts.
- Notion search ranks on page properties only, so each runbook's `Alert Name`
  property must list every alert its body covers.
- Slack is the action surface; the console observes and configures. A
  cluster's Settings LLM key overrides `ANTHROPIC_API_KEY` in `.env`.
- Any new artifact field needs a round-trip test through the real producer
  and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios, oracles, structured grading, paired stats,
  release gate); two paid campaigns, both NOT_DEMONSTRATED. Do not quote
  #70's MTTR.
- E2E Runs 1–9 (2026-09-28/30): all defects fixed through `196eb31`;
  negative control NO_ACTION_CORRECT in Runs 3–9.
- Branch `judge-calibration` (pushed to `ccb7956`; not merged, no PR):
  calibration review-set builder and transcript store, judge design
  (`evals/benchmarks/graders/CALIBRATION_DESIGN.md`), demo script,
  specialist model anchor (a7ec4ca), runbook-only remediation.
- 2026-10-02: Codespace image rebuilt from this branch (runbook-only live;
  rollback tag `sentinel/api:pre-runbook-only`). Live Notion corpus checked:
  the four Meridian pages match `examples/meridian/runbooks/`; 17 RB-AUTO
  drafts; 5 generic pages authorize nothing. OOM and High Latency `Alert Name`
  properties corrected and read back.
- Review set (2026-10-01): `reports/judge-calibration/`, 25 blinded cases
  (6 negative-control), 114 transcripts attached. Key and map are 0600.
- `test_runbook_scenario_coverage.py`: each v3 scenario needing a fix has a
  prescribing runbook.
- `locatable` pre-check (`benchmarks/evidence_locator.py`), offline-tested.

## Active problem
- **Rehearsal blocked** (attempt 2026-10-02, ≈$0): every model call got 401
  `API key is invalid` — the `.env` key has an `sk-ant-usr-` prefix, not a
  Console key. The cluster's stored key was cleared that day, so `.env` is
  the only source. Slack posts fail `not_in_channel` in `#all-meridian`.
- **No run can grade better than `INCOMPLETE`**:
  `structured_grading._semantic_criterion` is a shape check with no judge.

## Relevant files
- `src/sre_agent/{runbook_authority,graph_builder,approval_flow,policy_gate,
  mutation_gateway}.py`, `docs/DEMO.md`.
- `evals/benchmarks/{sre_bench,calibration_cases,transcript_store}.py`.
- `infra/local/docker-compose.yaml` (project `platform`).
- Meridian repo `/workspaces/meridian-shop-deploy` (3 commits ahead, unpushed).

## Verification commands and latest results
- Work happens on Codespace `cuddly-winner-659v67gv695hrxjw` (shut down
  since 2026-10-02); after a restart run `scripts/dev/codespace_boot.sh`.
  It has no git credentials: push via a git bundle from the Mac.
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2642 passed,
  6 skipped (2026-10-02, `judge-calibration`).
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api`, then `up -d --no-build --no-deps
  --force-recreate temporal-worker sre-agent-api`. API health: `/ping` on 8080.
- Single trial: the `docs/DEMO.md` command. It needs `BENCH_ALLOW_HOLDOUT=1`
  and `BENCH_INCIDENT_TIMEOUT_SEC=2700` (default 300s is below the 1260s
  recovery floor); secrets from `/home/vscode/bench.env` (key names only).
  SIGINT does not stop a trial early — it runs to its timeout and cleans up.
- Edge MCP: `docker compose -p edge_mcp_servers` from `services/edge_mcp_servers`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price every run first.
  ≈$11.60 spent through Run 9. Calibration campaign (~$40+) not authorized.
- `master` (`b09933c`) lacks `judge-calibration`: rebuilding the image from
  the main checkout drops runbook-only remediation until merged.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- 3 of 22 scenarios need deploy/rollback evidence no injection creates.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged `checkout-service/app.py`, `.last_deployed_sha` and
  `.local-baseline-restore.md` alone. Wait ≥5 min after cluster changes
  before injecting.
- kube-state-metrics' ClusterRole can list/watch secrets (faithful to live).

## Next bounded task
1. User: put a valid Console key in Settings (or `.env` + container
   restart) and `/invite` the bot to `#all-meridian`.
2. On approval, rehearse Act 1 (~$1.00–1.50, stop at $2); leave
   `BENCH_AUTO_APPROVE` unset to rehearse the Slack approval click.
3. With sign-off, open a PR merging `judge-calibration` into `master`.
4. Free judge work: hand-label the 25 cases (two blind passes ≥48h apart),
   adjudicate, freeze. Run `python -m benchmarks.evidence_locator` on the real
   review set; then perturbation generator and a judge harness verified
   against a stub judge. Only then price a judge pass.
