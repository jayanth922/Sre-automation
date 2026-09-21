# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible and cost-conscious SRE
agent. Deterministic policy and durable state—not model prose—control writes,
approvals, status transitions and operator-facing claims.

## Current milestone
**Platform implementation is closed; rigorous AI evidence is next.** The
current outcome is a reproducible study of whether specialist decomposition,
reflection and memory improve incident diagnosis enough to justify their
latency and cost. Do not add platform breadth unless it blocks that study.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant, idempotency and audit boundary. Approval cannot override a
  hard block; successful actions are not replayed after process death; a
  cleared incident cannot restart remediation (#40).
- Strict breadth gates apply only to specialist ReAct reads. Deterministic
  verification retains tenant isolation without model-search limits. Scope
  refusals audit as `REFUSED` without cancelling sibling calls.
- Policy columns use nullable=inherited semantics and fail closed to production
  defaults. Losing the keystore volume makes credentials unrecoverable, so it
  must be backed up with the database.
- Benchmark evidence is content-addressed to scenario, code, configuration,
  rubric, model and trace artifacts. Missing or inconsistent evidence blocks a
  claim rather than degrading to a plausible summary.
- Production promotion remains tied to recovery, full structured quality,
  safety and complete cost traces. Diagnosis-only evidence cannot authorize a
  rollout or bypass human approval.

## Completed or verified work
- Context/query hardening reduced a live smoke from 16/142 successful tool
  calls to 53/59. Specialists receive exact alert labels/time, selected
  procedures and bounded prior findings; durable evidence remains lossless.
- The v2 benchmark has 22 versioned scenarios, an independent recovery oracle,
  structured grading, adversarial cases, confidence evaluation, paired
  statistics, four ablation arms and a content-addressed release gate.
- Dataset v3 adds three `missing_data` scenarios, one per split, and repins
  deterministically; v2 remains the default.
- Job completion has one durable owner; 529 retry is proven through the real
  env/builder/SDK chain; the emergency lock is wired and its read reports
  unavailable state honestly.
- **Diagnosis metric complete locally:** trial schema v3 requires the structured
  diagnosis criterion; missing/malformed input fails closed. Statistical
  reports expose versioned paired diagnosis results, and ablation report v2
  uses that metric while retaining recovery/quality separately. Approval-gated
  tests prove diagnosis can improve while both other outcomes remain zero.
  Release evidence recomputes the metric from raw rows and detects tampering.
- The memory preflight is now genuinely read-only: it suppresses collection
  creation and backfill, rejects absent tenant/cluster scope, and will not call
  an uninitialized semantic index active. The Mac dev stack measured 0/6
  observable dev scenarios (zero skills and incident memories); this is a
  diagnostic only, not evidence about the live Codespace stack.
- Smoke `b13ce2c5` cost $2.2079 over 69 model calls and stopped correctly at
  human approval. It predates the Phase C cost reduction and is not an
  end-to-end quality result.

## Active problem
No live statistical row has yet proven grade, confidence, cost and complete
trace persistence together. Autonomy also remains uncalibrated: Stage 1 needs
at least 40 `live_benchmark` diagnosis observations, then a content-addressed
artifact bound to the current configuration fingerprint.

## Relevant files
- Evaluation: `benchmarks/{scoring,statistical_eval,ablation_eval,sre_bench,
  structured_grading,release_evidence,release_gate}.py`.
- Agent/context: `sre_agent/{agent_nodes,namespace_scope,mcp_tool_wrapper,
  context_compaction,narrative,tracing,model_accounting}.py`.
- Safety/durability: `sre_agent/{mutation_gateway,act_phase,
  incident_remediation_workflow,approval_flow}.py`.
- Rationale: `docs/ai/{HANDOFF_CODEX,DECISIONS}.md`.

## Verification commands and latest results
- `uv run pytest -q` → **2099 passed, 6 skipped** (2026-09-20).
- Focused evaluator/recorder/ablation tests → **70 passed**.
- Release evidence/gate tests → **34 passed**; fixture regeneration check clean.
- Black and Ruff pass on all changed Python files.

## Known blockers or risks
- Dataset v3 is not runnable until Meridian's `metrics_enabled` knob is
  committed/pushed and its checkout image rebuilt in the separate repo.
- The authoritative tenant-scoped memory preflight has not run on Codespace;
  the local dev stack is blind and must not be used for a `no_memory` claim.
- Paid evidence is not authorized: about $88 for 40 calibration observations;
  about $194/$389/$583 for increasing ablation tiers at the stale smoke floor.
- Semantic causal/evidence criteria are not human-calibrated. Do not promote
  them to headline metrics until blinded labels measure agreement and publish
  disagreements/error slices.
- Grades before/after the #54 rubric digest change are not comparable.
- `/app/reports` is not a volume; export paid evidence before rebuilds.
- Never stage `.agents/`, `.env.local-backup-20260910`, `.env.bak-*`; never
  `git add -A`.

## Next bounded task
Commit/deploy the evaluator changes, then run the corrected tenant-scoped
preflight on the Codespace agent runtime. If its corpus is blind, prepare a
training-split-only benchmark corpus with explicit provenance; never seed from
dev or holdout. Only after the user authorizes a stage and budget, run exactly
one live statistical smoke to prove row persistence. Do not launch calibration
or the multi-arm campaign yet. Then build the blinded human-labelled semantic
grader set and publish exact revisions, raw evidence and failure slices.
