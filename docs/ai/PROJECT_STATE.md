# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Frontend operator experience.** The backend AI-engineering milestone is
implemented and verified: reproducible evaluation tooling, truthful negative
evidence, corrected specialist context and structural runtime cost bounds. The
next milestone is to review the web console against the actual incident and
approval workflows, then close the highest-impact usability gaps without
weakening backend invariants.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency and audit boundary. Successful actions are not
  replayed after process death, and a cleared incident cannot restart
  remediation (#40).
- Specialist reads are bounded; verification remains deterministic. Approval
  cannot override a hard block.
- Benchmark evidence is content-addressed to scenario, code, configuration,
  rubric, model and trace artifacts. Missing or inconsistent evidence blocks a
  claim.
- Recovery, structured quality, safety and complete cost traces govern release.
  Diagnosis-only evidence cannot authorize rollout or bypass approval.

## Completed or verified work
- The v2 benchmark has 22 scenarios, independent recovery oracles, structured
  grading, adversarial cases, confidence evaluation, paired statistics, four
  ablation arms and a content-addressed release gate. Dataset v3 adds three
  split-specific `missing_data` scenarios.
- Trial schema v3 and ablation report v2 measure diagnosis separately from
  recovery and quality; release evidence recomputes it from raw rows.
- A deterministic blinded-calibration builder now validates source/rubric
  digests, hides scenario provenance from reviewers and writes a
  content-addressed manifest without model calls. One case was packaged;
  it is not a calibrated dataset.
- A train/dev-only, one-scenario derived-dataset builder supports a single-row
  statistical harness smoke and refuses holdout input or overwrite.
- Codespace memory preflight: semantic path, 6/6 dev scenarios with skill hits,
  five skills in store, zero tenant incident-memory points. Skill retrieval is
  observable; incident recall is not, so `full` vs `no_memory` is incomplete
  causal evidence.
- One authorized smoke ran once with no retry: `inventory_slow_queries`, full
  arm, `investigated`/`UNRESOLVED`, diagnosis `FAIL`, safety passed, 206 complete
  trace spans, 1,540.78 seconds and $2.52633925 over 97 model calls. It exposed
  missing initial namespace context, an MCP runbook-wrapper parse bug, repeated
  reinvestigation and specialist timeouts. Focused fixes/tests now inject the
  runtime namespace and unwrap LangChain MCP text blocks.
- The smoke exposed a configuration-identity defect: a root-trace URI inside
  `tools` changed the fingerprint after the run started. The evaluator now
  excludes only that URI while retaining capture policy and schemas. Preflight
  and actual manifests now both recompute to `14cf72da…`. The original row is
  immutable and remains non-comparable; it proves persistence only.
- Specialist ReAct execution is now structurally cost-bounded: six model turns
  per specialist, one reflector-directed reinvestigation round, a derived
  recursion backstop, and the existing 120-second timeout. A limit hit
  closes the stream before another tool/model round, preserves partial evidence,
  skips cosmetic narration and records its counters. Limits are clamped,
  operator-configurable and fingerprinted in the run manifest. The default
  maximum across the initial and recheck specialist passes is 48 calls.
- The unused `/api/v1/chat` route and module are removed. Slack incident threads
  remain the sole conversation surface, eliminating an unbounded authenticated
  120-second agent invocation.
- Public negative evidence and limitations are in `docs/ai/AI_RESULTS.md`.
- Meridian checkout now has a tested `metrics_enabled` runtime switch so v3 can
  remove metrics while leaving health intact. The rebuilt image is not deployed.

## Relevant files
- Evaluation: `benchmarks/{calibration_cases,statistical_smoke_dataset,
  statistical_eval,sre_bench,ablation_eval}.py` and `docs/ai/AI_RESULTS.md`.
- Context fixes: `sre_agent/{agent_nodes,narrative,runbook_brief}.py`.
- Runtime limits: `sre_agent/{investigation_limits,run_manifest}.py`.
- Meridian: `services/checkout-service/app.py`,
  `testing/test_checkout_metrics_switch.py`.

## Verification commands and latest results
- `uv run pytest -q` → **2119 passed, 6 skipped** (2026-09-21).
- Targeted Ruff and Black checks pass on new/runtime/evaluator files.
- Meridian focused Ruff + pytest → **2 passed**.
- `git diff --check` passes. Codespace is stopped; exported evidence remains
  under ignored `reports/` paths.

## Known blockers or risks
- The smoke is not model-quality or ablation evidence and must never be counted
  as a comparable row. No paid rerun or campaign is authorized.
- Semantic criteria still lack two independent blinded labelers, adjudication
  and measured agreement. The single prepared case is only workflow proof.
- Incident-memory recall is absent in the measured tenant. Do not claim a full
  memory ablation until a training-only, provenance-pinned corpus exists.
- Dataset v3 needs the Meridian image rebuilt and deployed.
- Rotate the Anthropic API key and Slack app token exposed in terminal output
  during this session.
- Never stage `.agents/`, `.env.local-backup-20260910` or `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Audit the frontend's incident detail, approval and investigation surfaces
against the current API and state model. Define one bounded usability slice,
implement it and verify it with the existing frontend quality gates. Keep
benchmark campaigns paused until explicit budget authorization.
