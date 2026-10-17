# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** (`docs/DEMO.md`, Act 1 `payment_provider_outage`) —
   key, routing and Slack ready; waiting on the user's launch.
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
  tool; otherwise it escalates with `runbook_gap`. No applicable runbook
  means the model is never asked. `RB-AUTO` pages are drafts.
- Each Notion runbook's `Alert Name` property must list every alert its body
  covers (search ranks on properties only).
- Slack is the action surface; the console observes and configures. A
  cluster's Settings LLM key overrides `ANTHROPIC_API_KEY` in `.env`.
- A cluster with dynamic routing on has no static model (e75cb39):
  `resolve_llm` ignores a stored one, create/update clear it, the console
  hides the picker. Tiers: protected `model_router._ANTHROPIC_TIER_DEFAULTS`
  (haiku-4-5 / sonnet-5 / opus-5).
- Any new artifact field needs a round-trip test through the real producer
  and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios, oracles, structured grading, paired stats,
  release gate); two paid campaigns, both NOT_DEMONSTRATED. Do not quote
  #70's MTTR.
- PR #57 (merged): calibration review-set builder, transcript store, judge
  design (`evals/benchmarks/graders/CALIBRATION_DESIGN.md`), demo script,
  runbook-only remediation. Live Notion runbooks match
  `examples/meridian/runbooks/`.
- Review set rebuilt 2026-10-16 (`reports/judge-calibration/`, 25 cases, 143
  transcripts incl. 29 K8s prescans). Never label `superseded-2026-10-01/`.
- Locator 11 PASS / 0 FAIL / 14 UNCHECKABLE; 96 perturbed copies;
  `judge_view` leak-free (`CALIBRATION_DESIGN.md` §2–4).
- PR #58 (merged, 7483411): no static model when routed; migration
  `b8c9d0e1f2a3` cleared `kind-meridian`'s model; live picks checked.
- PR #59 (merged, 8c997ed): `nl_query` routes; an AST test keeps LLM
  construction in the router modules. `.env` has no `ANTHROPIC_MODEL`.
- meridian-shop master `7bcfb45`: the Codespace's 3 commits, rebased onto
  `cefa745` and pushed 2026-10-17.

## Active problem
- **Rehearsal**: `.env` key works (user keeps it); Slack bot `meridian` is
  valid and in `C0C0KA474LC` (`#all-meridian`).
- **No run can grade better than `INCOMPLETE`**:
  `structured_grading._semantic_criterion` is a shape check with no judge.

## Relevant files
- `src/sre_agent/{runbook_authority,graph_builder,approval_flow,policy_gate,
  mutation_gateway}.py`, `docs/DEMO.md`.
- `evals/benchmarks/{sre_bench,calibration_cases,transcript_store,
  evidence_locator,perturbations,judge_view}.py`.
- `infra/local/docker-compose.yaml` (project `platform`).
- Meridian repo `/workspaces/meridian-shop-deploy` (local master keeps the
  3 commits' pre-rebase SHAs).

## Verification commands and latest results
- Work happens on Codespace `cuddly-winner-659v67gv695hrxjw`; after a restart
  run `scripts/dev/codespace_boot.sh`. It has no git credentials: push via a
  git bundle from the Mac. Its `/workspaces/Sre-automation` checkout has
  files copied in (sha256-checked) from e75cb39, PR #59 and the watcher
  change. Eval modules run
  from `/home/vscode/judge-run/evals`. Container Python is `uv run python`.
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2721 passed,
  6 skipped (2026-10-17, PR #59); release gate `NOT_REQUIRED`; dashboard
  `tsc --noEmit` 0 errors. API and worker rebuilt ≈20:00Z.
- Rebuild: `docker compose -p platform -f infra/local/docker-compose.yaml
  build temporal-worker sre-agent-api dashboard`, then `up -d --no-build
  --no-deps --force-recreate` the same three. The API runs `alembic upgrade
  head` on start. API health: `/ping` on 8080.
- Single trial: the `docs/DEMO.md` command (timeout 2700s; 300s is below
  the recovery floor), secrets from `/home/vscode/bench.env`. SIGINT does not
  stop a trial early.
- Edge MCP: `docker compose -p edge_mcp_servers` from `services/edge_mcp_servers`.

## Known blockers or risks
- Budget: no paid run without explicit approval; price every run first.
  ≈$11.60 spent through Run 9. Calibration campaign (~$40+) not authorized.
- FAST (haiku-4-5) is untested in a paid run. `sonnet-4-5` (ladder only)
  retires 2026-11-30; haiku-4-5 may be deprecated any time (`haiku-5-5`).
- 3 of 22 scenarios need deploy/rollback evidence no injection creates.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged `checkout-service/app.py`, `.last_deployed_sha` and
  `.local-baseline-restore.md` alone. Wait ≥5 min after cluster changes
  before injecting.
- `watch_meridian_deploy.sh` is opt-in (`MERIDIAN_WATCHER_ENABLED=1`): it
  `reset --hard`s to GitHub master, which still has `4ed89b2`/`7a6e223`
  (checkout ~84% 5xx), wiping the healthy-checkout restore.
- kube-state-metrics' ClusterRole can list/watch secrets (faithful to live).

## Next bounded task
1. User launches Act 1 (~$1.00–1.50, stop at $2). Leave
   `BENCH_AUTO_APPROVE` unset to rehearse the Slack approval click.
2. On the user's "push", commit the watcher opt-in change and open its PR.
3. User: hand-label the 25 rebuilt cases (two blind passes ≥48h apart),
   adjudicate, freeze.
4. Free, in a fresh session: judge harness + `CALIBRATION_DESIGN.md` §5
   metrics on the judge views (locator FAIL decides evidence_support;
   over-budget views abstain), verified on a stub judge. Then price a pass.
