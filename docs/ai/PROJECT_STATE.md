# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Frontend operator experience — audited.** The backend milestone is verified.
Every route decorator under `sre_agent/api/v1` was matched against every call
site in `dashboard/`, both directions: 40 of 51 endpoints have a caller, the
other 11 deliberate or redundant. A second pass found what endpoint coverage
cannot — *which record* a page picks from an org-wide list.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- Specialist reads are bounded; verification remains deterministic. Approval
  cannot override a hard block.
- **A live feed is org-scoped; narrowing it to one cluster is the consumer's
  job.** `event_visible_to_org` is fail-closed, so tenant isolation never
  depended on the client — but `/ws/insights` and `/ws/incidents` carry every
  cluster in the org. Lifecycle payloads carry `cluster_id`; a consumer that
  cannot place an event drops it.
- Benchmark evidence is content-addressed to scenario, code, config, rubric,
  model and traces; inconsistent evidence blocks a claim. Release needs
  recovery, quality, safety and complete cost traces — diagnosis alone
  authorizes nothing.

## Completed or verified work
- v2: 22 scenarios, independent recovery oracles, structured grading,
  adversarial cases, paired statistics, four ablation arms, a content-addressed
  release gate. v3 adds three split-specific `missing_data` scenarios.
- Trial schema v3 and ablation report v2 separate diagnosis from recovery and
  quality; release evidence recomputes it from raw rows.
- A deterministic blinded-calibration builder validates digests, hides
  provenance and writes a content-addressed manifest with no model calls. One
  case is packaged; that is not a calibrated dataset.
- Memory preflight: 6/6 dev scenarios hit skills, five skills stored, zero
  tenant incident-memory points.
- One authorized smoke, no retry: `inventory_slow_queries`, full arm,
  `investigated`/`UNRESOLVED`, diagnosis `FAIL`, 1,540.78s, **$2.5263** over 97
  model calls. It exposed four defects — namespace context, an MCP parse bug,
  repeated reinvestigation, a fingerprint moving mid-run — all since fixed.
  That row stays non-comparable.
- Specialist ReAct execution is structurally cost-bounded: six model turns
  each, one reinvestigation round, a recursion backstop, the 120s timeout, a
  48-call default ceiling. A limit hit keeps partial evidence and records its
  counters. See DECISIONS.
- `/api/v1/chat` is removed; Slack threads are the sole conversation surface.
- Console audit: 0 calls to absent endpoints, 0 dead links, 0 stubs, 19/19
  pages handling loading/error/empty, clean `next build`. Two honesty fixes:
  the Settings preflight graded the env `GITHUB_TOKEN` rather than the cluster
  PAT, and the cluster picker rendered a failed `GET /clusters` as "none".
- Two cross-cluster render leaks closed: Insights fell back to a sibling's
  snapshot and never filtered its sweep feed; incident toasts, on every cluster
  page, showed siblings' alert names and linked to a certain 404. Tests pin
  both.
- SLOs are deletable from the console: a two-step confirm naming what is
  destroyed, and the dashboard's first `api.delete`.

## Relevant files
- Evaluation: `benchmarks/{calibration_cases,statistical_smoke_dataset,
  statistical_eval,sre_bench,ablation_eval}.py`; `docs/ai/AI_RESULTS.md` holds
  the public negative evidence and limitations.
- Stream scoping: `sre_agent/live_events.py`,
  `dashboard/components/console/IncidentToasts.tsx`.
- Runtime limits: `sre_agent/{investigation_limits,run_manifest}.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2138 passed, 6 skipped** (2026-09-22).
- Dashboard gates: `tsc --noEmit` clean, `npm run build` compiles every route,
  eslint clean on changed files. The repo-wide baseline stays red (30
  pre-existing `react-hooks/set-state-in-effect`); no change adds one.
- Targeted Ruff and Black checks pass on new/runtime/evaluator files.
- `git diff --check` passes; exported evidence stays under ignored `reports/`.

## Known blockers or risks
- No paid rerun or campaign is authorized.
- **The toast fix is half-live.** Stamping `cluster_id` is a producer change
  baked into the API image, so until a deploy no lifecycle payload carries one
  and the console shows *no* incident toasts rather than the wrong cluster's.
  Deliberate; deploy is not authorized.
- Semantic criteria still lack two independent blinded labelers, adjudication
  and measured agreement. The single prepared case is only workflow proof.
- Incident recall is unobservable, so `full` vs `no_memory` is incomplete.
- Dataset v3 is not runnable: `services/checkout-service/app.py` has the
  `metrics_enabled` switch but the Meridian image is not rebuilt, so injecting
  the fault would silently measure nothing.
- Rotate the Anthropic API key and Slack app token exposed in terminal output.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Pin the remaining nine `useLiveStream` consumers: they use events only as
refresh triggers, which is why they are clean — a property worth asserting
rather than assuming. Cluster-level deletes stay API-only by choice.

Then benchmarking, on the user's stage and budget. Keep campaigns paused until
explicit budget authorization.
