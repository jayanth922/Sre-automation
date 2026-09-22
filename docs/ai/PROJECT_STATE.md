# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Frontend operator experience — audited.** The backend milestone is
implemented and verified. On 2026-09-21 every route decorator under
`sre_agent/api/v1` was matched against every call site in `dashboard/`, in
*both* directions: 40 of 51 endpoints have a caller, the other 11 deliberate
or redundant. Two honesty defects it surfaced are fixed.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- Specialist reads are bounded; verification remains deterministic. Approval
  cannot override a hard block.
- Benchmark evidence is content-addressed to scenario, code, configuration,
  rubric, model and trace artifacts; inconsistent evidence blocks a claim.
  Release needs recovery, structured quality, safety and complete cost traces —
  diagnosis alone cannot authorize rollout or bypass approval.

## Completed or verified work
- v2: 22 scenarios, independent recovery oracles, structured grading,
  adversarial cases, paired statistics, four ablation arms, a content-addressed
  release gate. v3 adds three split-specific `missing_data` scenarios.
- Trial schema v3 and ablation report v2 measure diagnosis separately from
  recovery and quality; release evidence recomputes it from raw rows.
- A deterministic blinded-calibration builder validates source/rubric digests,
  hides provenance from reviewers and writes a content-addressed manifest with
  no model calls. One case is packaged; that is not a calibrated dataset.
- A train/dev-only one-scenario dataset builder supports a single-row harness
  smoke and refuses holdout input or overwrite.
- Memory preflight: 6/6 dev scenarios hit skills, five skills stored, zero
  tenant incident-memory points.
- One authorized smoke, no retry: `inventory_slow_queries`, full arm,
  `investigated`/`UNRESOLVED`, diagnosis `FAIL`, 1,540.78s, **$2.5263** over 97
  model calls. It exposed missing namespace context, an MCP runbook-wrapper
  parse bug and repeated reinvestigation — all since fixed — plus a
  configuration-identity defect where a root-trace URI inside `tools` moved the
  fingerprint mid-run, since narrowed. That row stays non-comparable.
- Specialist ReAct execution is structurally cost-bounded: six model turns per
  specialist, one reinvestigation round, a derived recursion backstop, the
  existing 120s timeout. A limit hit closes the stream, keeps partial evidence
  and records its counters. Limits are clamped, configurable
  and fingerprinted; the default ceiling is 48 specialist calls. See DECISIONS.
- The unused `/api/v1/chat` route and module are removed. Slack incident threads
  remain the sole conversation surface, eliminating an unbounded authenticated
  120-second agent invocation.
- Console audit: 0 calls to absent endpoints, 0 dead links, 0 stubs, 19/19
  pages handling loading/error/empty, clean `next build`. Two fixes — the
  Settings preflight graded `GITHUB_TOKEN` from the environment, not the
  per-cluster PAT `from_cluster` uses — red for a tenant who configured one,
  green for one who did not, on the page that reports whether setup is wired;
  and the cluster picker rendered a failed `GET /clusters` as "you have none".
- Meridian checkout has a tested `metrics_enabled` switch, so v3 can remove
  metrics while leaving health intact.

## Relevant files
- Evaluation: `benchmarks/{calibration_cases,statistical_smoke_dataset,
  statistical_eval,sre_bench,ablation_eval}.py`; `docs/ai/AI_RESULTS.md` holds
  the public negative evidence and limitations.
- Context fixes: `sre_agent/{agent_nodes,narrative,runbook_brief}.py`.
- Runtime limits: `sre_agent/{investigation_limits,run_manifest}.py`.
- Meridian: `services/checkout-service/app.py`,
  `testing/test_checkout_metrics_switch.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2124 passed, 6 skipped** (2026-09-21).
- Dashboard gates: `tsc --noEmit` clean, `npm run build` compiles every route,
  eslint clean on changed files. The repo-wide baseline stays red (30
  pre-existing `react-hooks/set-state-in-effect`); no change adds one.
- Targeted Ruff and Black checks pass on new/runtime/evaluator files.
- Meridian focused Ruff + pytest → **2 passed**.
- `git diff --check` passes; exported evidence stays under ignored `reports/`.

## Known blockers or risks
- No paid rerun or campaign is authorized.
- Semantic criteria still lack two independent blinded labelers, adjudication
  and measured agreement. The single prepared case is only workflow proof.
- Incident recall is therefore unobservable, so `full` vs `no_memory` is
  incomplete causal evidence. Do not claim a full memory ablation until a
  training-only, provenance-pinned corpus exists.
- Dataset v3 is not runnable: the Meridian image is not rebuilt or deployed,
  so injecting the fault would silently measure nothing.
- Rotate the Anthropic API key and Slack app token exposed in terminal output
  during this session.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
The audit is closed; what is left there is a product choice, not wiring.
`DELETE /clusters/{id}`, `/clusters/{id}/incidents` and
`/clusters/{id}/slos/{slo_id}` have no console caller, so an SLO can be created
and edited from the UI but removed only by direct API call. Decide whether the
SLOs page gets a delete control, or pin the absence with a test the way
`jobs/trigger` is pinned.

Then benchmarking, on the user's stage and budget. Keep campaigns paused until
explicit budget authorization.
