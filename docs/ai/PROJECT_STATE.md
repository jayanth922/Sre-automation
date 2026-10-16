# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** (`docs/DEMO.md`, Act 1 `payment_provider_outage`) —
   key fixed; waiting on the Slack invite and spend approval.
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
- Branch `judge-calibration`, merged into `master` by PR #57: calibration
  review-set builder and transcript store, judge design
  (`evals/benchmarks/graders/CALIBRATION_DESIGN.md`), demo script,
  runbook-only remediation.
- 2026-10-02: Codespace image rebuilt from this branch (rollback tag
  `sentinel/api:pre-runbook-only`); live Notion runbooks match
  `examples/meridian/runbooks/`, `Alert Name` properties corrected.
- Review set rebuilt 2026-10-16 (`reports/judge-calibration/`, same key, same
  25 case ids): 143 transcripts attached, 29 of them K8s prescan transcripts
  found by `root_trace_id` (ba73dce). The 2026-10-01 copy lacked them and is
  in `superseded-2026-10-01/` — never label it.
- `test_runbook_scenario_coverage.py`: each v3 scenario needing a fix has a
  prescribing runbook.
- On the real set: locator 11 PASS / 0 FAIL / 14 UNCHECKABLE; 96 perturbed
  copies; `judge_view` (6dbd932) checked leak-free. Details:
  `CALIBRATION_DESIGN.md` §2–4; commands: `graders/README.md`.

## Active problem
- **Rehearsal**: the `.env` key works again (2026-10-16, two 1-token probes
  returned 200; the earlier 401's cause is unknown). `ANTHROPIC_MODEL` in `.env`
  is retired (404) but unused: the cluster passes `claude-sonnet-5`. Slack
  posts failed `not_in_channel` (bot not in `#all-meridian`).
- **No run can grade better than `INCOMPLETE`**:
  `structured_grading._semantic_criterion` is a shape check with no judge.

## Relevant files
- `src/sre_agent/{runbook_authority,graph_builder,approval_flow,policy_gate,
  mutation_gateway}.py`, `docs/DEMO.md`.
- `evals/benchmarks/{sre_bench,calibration_cases,transcript_store,
  evidence_locator,perturbations,judge_view}.py`.
- `infra/local/docker-compose.yaml` (project `platform`).
- Meridian repo `/workspaces/meridian-shop-deploy` (3 commits ahead, unpushed).

## Verification commands and latest results
- Work happens on Codespace `cuddly-winner-659v67gv695hrxjw`; after a restart
  run `scripts/dev/codespace_boot.sh`. It has no git credentials: push via a
  git bundle from the Mac. Its `/workspaces/Sre-automation` checkout is on
  `master`; the new eval modules run from `/home/vscode/judge-run/evals`
  first on `PYTHONPATH` (copied file by file, sha256-checked).
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2714 passed,
  6 skipped (2026-10-16, `judge-calibration` at 564f243).
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api`, then `up -d --no-build --no-deps
  --force-recreate temporal-worker sre-agent-api`. API health: `/ping` on 8080.
- Single trial: the `docs/DEMO.md` command (`BENCH_ALLOW_HOLDOUT=1`,
  `BENCH_INCIDENT_TIMEOUT_SEC=2700`; 300s is below the recovery floor);
  secrets from `/home/vscode/bench.env`. SIGINT does not stop a trial early.
- Edge MCP: `docker compose -p edge_mcp_servers` from `services/edge_mcp_servers`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price every run first.
  ≈$11.60 spent through Run 9. Calibration campaign (~$40+) not authorized.
- The specialist model anchor (a7ec4ca) was reverted before PR #57 merged:
  `agent_nodes.py` is a protected tool file and the release gate blocks it
  without a release-evidence bundle. No effect while the cluster uses
  `claude-sonnet-5`; the Codespace image (2026-10-02) still contains it.
- Rotate the Anthropic key and Slack token exposed in terminal output.
- 3 of 22 scenarios need deploy/rollback evidence no injection creates.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged `checkout-service/app.py`, `.last_deployed_sha` and
  `.local-baseline-restore.md` alone. Wait ≥5 min after cluster changes
  before injecting.
- kube-state-metrics' ClusterRole can list/watch secrets (faithful to live).

## Next bounded task
1. User: `/invite` the bot to `#all-meridian`.
2. On approval, rehearse Act 1 (~$1.00–1.50, stop at $2); leave
   `BENCH_AUTO_APPROVE` unset to rehearse the Slack approval click.
3. Re-land the model anchor in its own PR (revert its revert) once release
   evidence exists; that evidence is a paid run, so price it first.
4. User: hand-label the 25 rebuilt cases (two blind passes ≥48h apart),
   adjudicate, freeze.
5. Free: judge harness and §5 metrics of `CALIBRATION_DESIGN.md` on the
   judge views, combined with the locator (a locator FAIL decides
   evidence_support), verified against a stub judge; an over-budget view
   abstains. Only then price a judge pass.
