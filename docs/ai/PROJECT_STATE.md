# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, cost-conscious, and
production-operable. Deterministic policy and durable state—not model prose—
control writes, approvals, status transitions, and operator-facing claims.

## Current milestone
**Frontend wiring, re-audited by path.** Backend verification is closed. The
dashboard's coverage was measured against every route decorator under
`sre_agent/api/v1` instead of by module name: **42 of 56 endpoints have a
caller**, and the one genuinely stranded feature — the emergency lock — is
now wired. A fresh install still needs no `.env` (keystore volume, first-run
claim page, `environment` / `approval_ttl_minutes` on the cluster row via
migration `a7b8c9d0e1f2`).

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant, idempotency, and audit boundary: approval cannot override a
  hard block, successful actions are not replayed after process death, and a
  cleared incident cannot restart remediation (#40).
- Strict read gates activate only for specialist ReAct calls; deterministic
  verification keeps tenant isolation but bypasses breadth limits. Scope
  refusals audit as `REFUSED` without cancelling sibling calls.
- Policy columns are **nullable = inherit**; resolution fails closed to
  `production` / 30 minutes and a typo is a 422, not a silent normalisation.
- **Decisions go through Slack, status comes back through the console.**
  `/approve`, `/mark-resolved` and the remediation-gate `decide` endpoint have
  no dashboard caller *by design* — the incident page renders the instruction
  to reply in the Slack thread. Do not build approval buttons.
- **The emergency lock is enforced and its read is now honest.**
  `is_cluster_locked` fails open, so `GET /lock` reports `state_available`
  beside `locked` and the console keeps "unknown" distinct from "released".
  Enforcement never depended on that: the gateway rejects `state_unavailable`
  before it consults the lock.
- Losing the keystore volume makes every stored credential unrecoverable —
  the accepted cost of zero-config boot. Back it up.

## Completed or verified work
- Job completion has a single owner: the second writer is deleted, not left
  unused, and a losing writer raises `DurableJobError` after commit.
- #44 proven without an outage — the real env → builder → SDK chain driven
  against a fake `[529, 529, 200]` transport, plus a zero-budget twin that
  proves the harness can fail.
- Dataset **v3** authored at $0: v2's 22 scenarios plus three `missing_data`,
  one per split, strict-loader clean and byte-identical on `--repin`. v2 is
  untouched and still the default.
- Emergency lock wired end to end, fail-open read fixed, call sites pinned.
- Calibration gate (`2de46bb`): an artifact that blocked its own autonomy
  threshold no longer counts as calibration.
- Smoke `b13ce2c5`: **$2.2079**, 69 model calls — taken before Phase C cut
  cost per model call 62%, so a stale ceiling.

## Active problem
Autonomy is gated by calibration, not a runtime bug: uncalibrated confidence
escalates SEV2 to SEV1, which needs approval and leaves trials unresolved.
Stage 1 needs ≥40 `live_benchmark` diagnosis observations under
`STATISTICAL_RECORDING`, then the artifact and fingerprint. `quality_success`
still requires recovery + PASS + safety, so both stay zero in every arm until
`statistical_eval` / `ablation_eval` change.

## Relevant files
- Frontend: `dashboard/components/console/BreakGlass.tsx`,
  `dashboard/app/(dashboard)/clusters/[id]/{layout,settings/page}.tsx`.
- Query/context: `sre_agent/{namespace_scope,mcp_tool_wrapper,agent_nodes,
  context_compaction,audit_context}.py`, `config/agent_config.yaml`.
- Benchmark: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench}.py`,
  `benchmarks/datasets/{v2,v3}/`.
- Operations and rationale: `docs/ai/HANDOFF_CODEX.md`, `DECISIONS.md`.

## Verification commands and latest results
- Full suite in the Codespace: **2090 passed, 6 skipped** (`.venv/bin/python
  -m pytest -q`). `uv sync --frozen --extra dev` alone fails 7 — add
  `--extra temporal --extra anthropic`.
- `dashboard/node_modules/.bin/tsc --noEmit` clean; `eslint` red repo-wide
  (30 errors, endemic `react-hooks/set-state-in-effect`), new files add none.
- Migration `a7b8c9d0e1f2` up/down/up against a throwaway DB. Live
  `sre_platform` is still at the old head.
- `audit_runbook_coverage.py` → 22/22; runbooks live in Notion.

## Known blockers or risks
- **v3 is not runnable yet.** Its `metrics_enabled` knob lives in
  `jayanth922/meridian-shop` (`services/checkout-service/app.py`),
  uncommitted and unpushed, and the checkout image is not rebuilt. Injecting
  it today is a no-op, so a v3 run would silently measure nothing.
- `STATISTICAL_RECORDING` is unit-verified but has never produced a row from
  a live trial on this stack.
- `remediation_gate_approvals` is 0 against 61 `approval_requests`: its only
  writer runs for **code-fix** actions (`graph_builder.py:731`) and v2
  scenarios are infra faults. A second subsystem, not a broken one.
- Grades are not comparable across the #54 fix (rubric sha256 moved).
- Never stage `.agents/`, `.env.local-backup-20260910`, `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Order is the user's: backend, then frontend, then benchmarking.

**Frontend, remaining — decisions, not wiring.** `POST /chat` is mounted and
spends a 120-second agent invocation for any signed-in member, while task #7
was explicitly "no chat": decide whether it stays mounted. `POST
/clusters/{id}/trigger` is absent for the same reason `/jobs/trigger` is, but
only `/jobs/trigger` has a test saying so. `GET /clusters/{id}/health` and the
standalone job `manifest` GET are redundant — that data already rides in
`ClusterResponse` and `JobResponse.run_manifest`.

**Then benchmarking**, on the user's stage and budget. Nothing paid is
authorized: not the ~$88 calibration, not the $194 / $389 / $583 tiers.
`ablation_eval.py:319` pins `code_sha` across arms, so land every pending fix
first. The free pre-flight (`benchmarks/ablation_coverage.py` and the rest)
has not been run.

`docs/ai/HANDOFF_CODEX.md` → "Step 3 — wire the frontend" holds the full
endpoint-by-endpoint result and the traps.
