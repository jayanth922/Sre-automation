# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Frontend operator experience — closed.** The backend milestone is verified.
Route decorators under `sre_agent/api/v1` were matched against call sites in
`dashboard/` both directions; a second pass found what endpoint coverage
cannot — *which record* a page picks from an org-wide list.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- Specialist reads are bounded; verification remains deterministic. Approval
  cannot override a hard block.
- **Org scope is not cluster scope.** `event_visible_to_org` is fail-closed
  and `get_owned_incident` joins on org, so tenant isolation holds — but
  `/ws/insights`, `/ws/incidents` and every `/incidents/{id}` route serve the
  whole org. Narrowing to one cluster is the consumer's job. Lifecycle
  payloads carry `cluster_id`; a consumer that cannot place an event drops
  it.
- Benchmark evidence is content-addressed to scenario, code, config, rubric,
  model and traces; inconsistent evidence blocks a claim. Release needs
  recovery, quality, safety and complete cost traces — diagnosis alone
  authorizes nothing.

## Completed or verified work
- v2: 22 scenarios, independent recovery oracles, structured grading,
  adversarial cases, paired statistics, four ablation arms, a content-addressed
  release gate. v3 adds three `missing_data` scenarios, one per split.
- Trial schema v3 and ablation report v2 separate diagnosis from recovery and
  quality; release evidence recomputes it.
- The blinded-calibration builder validates digests, hides provenance and
  writes a content-addressed manifest with no model calls. One case is
  packaged; that is not a calibrated dataset.
- Memory preflight: 6/6 dev scenarios hit skills, five stored, zero tenant
  incident-memory points.
- One authorized smoke, no retry: `inventory_slow_queries`, full arm,
  `investigated`/`UNRESOLVED`, diagnosis `FAIL`, 1,540.78s, **$2.5263** over 97
  model calls. It exposed four defects, all since fixed. That row stays
  non-comparable.
- Specialist ReAct is cost-bounded: six model turns each, one reinvestigation
  round, a recursion backstop, a 120s timeout, a 48-call ceiling. A limit hit
  keeps partial evidence. See DECISIONS.
- Console audit: 0 calls to absent endpoints, 0 dead links, 0 stubs, 19/19
  pages handling loading/error/empty, clean `next build`. Two honesty fixes:
  the Settings preflight graded the env `GITHUB_TOKEN`, not the cluster PAT;
  the cluster picker rendered a failed `GET /clusters` as "none".
- Three cross-cluster render defects closed: Insights fell back to a
  sibling's snapshot, toasts showed siblings' alert names on every cluster
  page, and because `get_owned_incident` joins on org rather than cluster,
  `/clusters/A/incidents/<B's>` rendered B's investigation under A's
  breadcrumb. Tests pin all three; the nine other stream consumers are pinned
  as payload-free.
- SLOs are deletable from the console: a two-step confirm naming what is
  destroyed, and the dashboard's first `api.delete`.
- The dashboard chat surface is gone: `POST /incidents/{id}/message` and both
  unimported components that called it. `handle_incident_message` survives for
  Slack thread replies, which is why its five tests moved onto it rather than
  out with the route.

## Relevant files
- Evaluation: `benchmarks/{calibration_cases,statistical_smoke_dataset,
  statistical_eval,sre_bench,ablation_eval}.py`; `docs/ai/AI_RESULTS.md` holds
  the public negative evidence and limitations.
- Stream scoping: `sre_agent/live_events.py`,
  `dashboard/components/console/IncidentToasts.tsx`.

## Verification commands and latest results
- `.venv/bin/python -m pytest -q` → **2140 passed, 6 skipped** (2026-09-22).
- Dashboard gates: `tsc --noEmit` clean, `npm run build` compiles every route.
  eslint is red repo-wide and was already red at HEAD on every file touched;
  no change adds an error.
- Ruff and Black pass on every new file; `agent_runtime.py` and
  `approval_flow.py` were already black-red at HEAD.

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
Frontend wiring is closed. Cluster-level deletes stay API-only by choice. So
is the @mention steer gap: war-room text takes one shared path from either
Slack event, deduplicated on `(channel, ts)` — see DECISIONS, "One Slack
message, one handler, one turn".

Then benchmarking, on the user's stage and budget. Keep campaigns paused until
explicit budget authorization.
