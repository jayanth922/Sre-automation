# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state — not model prose — control
writes, approvals, status transitions and operator-facing claims.

## Current milestone
1. **Demo rehearsal** (`docs/DEMO.md`, Act 1 `payment_provider_outage`) —
   key and routing fixed; waiting on the Slack invite and spend approval.
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
- Each Notion runbook's `Alert Name` property must list every alert its body
  covers (search ranks on properties only).
- Slack is the action surface; the console observes and configures. A
  cluster's Settings LLM key overrides `ANTHROPIC_API_KEY` in `.env`.
- A cluster with dynamic routing on has no static model (e75cb39):
  `resolve_llm` ignores a stored one, create/update clear it, the console
  hides the picker. Tiers come from `_ANTHROPIC_TIER_DEFAULTS` (haiku-4-5 /
  sonnet-5 / opus-5).
- Any new artifact field needs a round-trip test through the real producer
  and consumer.

## Completed or verified work
- v2 benchmark (22 scenarios, oracles, structured grading, paired stats,
  release gate); two paid campaigns, both NOT_DEMONSTRATED. Do not quote
  #70's MTTR.
- E2E Runs 1–9 (2026-09-28/30): defects fixed through `196eb31`.
- PR #57 (merged): calibration review-set builder, transcript store, judge
  design (`evals/benchmarks/graders/CALIBRATION_DESIGN.md`), demo script,
  runbook-only remediation. Live Notion runbooks match
  `examples/meridian/runbooks/`.
- Review set rebuilt 2026-10-16 (`reports/judge-calibration/`, 25 cases, 143
  transcripts incl. 29 K8s prescans). Never label `superseded-2026-10-01/`.
- Locator 11 PASS / 0 FAIL / 14 UNCHECKABLE; 96 perturbed copies;
  `judge_view` leak-free (`CALIBRATION_DESIGN.md` §2–4).
- `routing-no-static-model` (e75cb39, unpushed): migration `b8c9d0e1f2a3`
  cleared `kind-meridian`'s model; deployed to the Codespace 2026-10-17
  17:57Z, live per-task picks checked.

## Active problem
- **Rehearsal**: the `.env` key works; the user keeps it. Its retired
  `ANTHROPIC_MODEL` is unused (routed calls get tier models). Slack
  (2026-10-17): token and posting scopes valid (bot `meridian`), but
  `not_in_channel` for `C0C0KA474LC`, the channel past incidents posted to.
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
  `master` plus e75cb39's 7 files (copied, sha256-checked). Eval modules run
  from `/home/vscode/judge-run/evals`. Container Python is `uv run python`.
- `.venv/bin/python -m pytest -p no:cacheprovider -q` → 2718 passed,
  6 skipped (2026-10-17, `routing-no-static-model` at e75cb39); release gate
  `NOT_REQUIRED`; dashboard `tsc --noEmit` 0 errors.
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
- The reverted model anchor (a7ec4ca) is moot and gone from the image.
  FAST tasks now run on haiku-4-5 (was sonnet-4-5), untested in a paid run.
- 3 of 22 scenarios need deploy/rollback evidence no injection creates.
- Never stage `.agents/`, `.env.*` backups or `dashboard/`; never `git add -A`.
  Leave meridian's staged `checkout-service/app.py`, `.last_deployed_sha` and
  `.local-baseline-restore.md` alone. Wait ≥5 min after cluster changes
  before injecting.
- kube-state-metrics' ClusterRole can list/watch secrets (faithful to live).

## Next bounded task
1. User: `/invite @meridian` in `#all-meridian`; then re-run the read-only
   Slack check (`auth.test` + one `conversations.history` on `C0C0KA474LC`).
2. On approval, rehearse Act 1 (~$1.00–1.50, stop at $2), no earlier than
   18:02Z on 2026-10-17 (5 min after the rebuild); leave `BENCH_AUTO_APPROVE`
   unset to rehearse the Slack approval click.
3. On the user's "push", push `routing-no-static-model` and open its PR.
4. User: hand-label the 25 rebuilt cases (two blind passes ≥48h apart),
   adjudicate, freeze.
5. Free, in a fresh session: judge harness + `CALIBRATION_DESIGN.md` §5
   metrics on the judge views (locator FAIL decides evidence_support;
   over-budget views abstain), verified on a stub judge. Then price a pass.
