# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible, and cost-conscious
SRE agent. Deterministic policy and durable state—not model prose—control
writes, approvals, status transitions, and operator-facing claims.

## Current milestone
**Benchmarking, sized to the budget.** Backend correctness, console wiring,
repository layout, and the free coverage preflight are complete. The remaining
decision is which memory-seeding and ablation work merits paid trials.

## Current architecture and invariants
- Production packages live in `src/`, the console in `apps/`, MCP services in
  `services/`, evaluations in `evals/`, deployment assets in `infra/`, and the
  Meridian reference integration in `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole freshness, tenant,
  policy, idempotency, and audit boundary. Successful actions are not replayed
  after process death; cleared incidents cannot restart remediation.
- `policy_gate.decide()` hard-blocks before approval. Planner risk labels and
  other model/writer input cannot appeal deterministic policy.
- Org scope is not cluster scope. WebSocket feeds and bare incident routes are
  org-wide; every consumer must narrow records before rendering them.
- Tool evidence states the question and observation time it answers. Recovery
  is the scenario's Prometheus oracle, never the incident status.
- Slack is the conversational action surface. The console observes and
  configures; removed HTTP/dashboard chat paths cannot spend an agent turn.

## Completed or verified work
- Benchmark v2 has 22 scenarios, recovery oracles, structured grading,
  adversarial cases, paired statistics, four ablation arms, and a
  content-addressed release gate.
- Six authorized `inventory_slow_queries` trials cost about $1 each. Trials
  1–5 were unresolved; trial 6 was `VERIFIED_RECOVERED` with 958-second MTTR,
  independently confirmed recovery, and full root-cause, remediation,
  severity, and safety scores.
- Evidence fixes now retain specialist output across lanes, distinguish empty
  results from wrong questions, bind runbook probes to the current query,
  preserve chronology, and prevent unmeasured or stale evidence claims.
- Every metric read records `evaluated_at`; deployment specifications identify
  startup defaults rather than running state; narration cannot write
  “Unknown” after the reflector established evidence or a causal chain.
- Specialist execution remains bounded to six turns and one reinvestigation.
  Open-ended reads are refused and audited without cancelling sibling calls.
- Console wiring covers all 19 pages with truthful loading/error/empty states,
  cluster-scoped rendering, incident investigation start, on-demand audit
  logs, SLO deletion, and no mock data or dead links. All durable incident and
  remediation-gate statuses match backend enums.
- The repository has an OSS layout, license, contribution/security policies,
  indexed current docs, explicitly archived history, and regenerated diagrams.
  Obsolete screenshots, handoffs, demo chat components, and credential-bearing
  session notes are removed.

## Active problem
Coverage is measured but partial. The skill corpus has five verified skills
(crashloop ×2, latency, dependency, OOM) and no `high_error_rate` skill:
`no_memory` recall is 3/6 dev and 3/4 holdout. Incident memory has zero points,
so half that arm's intended removal is already absent. Seeding is a prerequisite
for a defensible memory ablation. Specialists still hit the six-turn ceiling.

## Relevant files
- Evaluation: `evals/benchmarks/{sre_bench,structured_grading,
  ablation_coverage,calibrate_semantic_floor}.py`.
- Evidence/runtime: `src/sre_agent/{agent_nodes,narrative,runbook_probe,
  runbook_queries,skill_store,investigation_limits}.py`.
- MCP evidence: `services/edge_mcp_servers/mcp_servers/{prometheus_real,
  loki_real,k8s_real}/server.py`.
- Console: `apps/dashboard/app/(dashboard)/clusters/[id]/`.

## Verification commands and latest results
- `uv run pytest -q` → 2,395 passed, 6 skipped.
- `bash scripts/ci/check_python_quality.sh` → lock, critical Ruff, curated
  mypy, and compile checks passed.
- Dashboard ESLint, TypeScript, and production build passed for all 19 pages.
- Quickstart smoke, secret scan, release-fixture freshness, shell syntax, and
  both Compose configurations passed.
- Free semantic calibration measured a 0.764 wrong-class ceiling and 0.851
  same-class floor; the configured 0.8 threshold rejected 161/161 wrong-class
  pairs and retained 70/70 same-class pairs. No model call was made.

## Known blockers or risks
- Rough paid tiers of $88–$632 are refused. No run beyond trial 6 is authorized.
- Semantic grading still needs calibrated blinded labels; two corpus records
  cannot satisfy a 100-sample floor.
- Dataset v3 lacks the rebuilt Meridian `metrics_enabled` image. Incident
  memory remains inert until a provenance-pinned corpus exists.
- Rotate exposed Anthropic, Slack, and NVIDIA credentials; deleting files does
  not remove secrets from published history.
- Never stage `.agents/`, environment backups, or use `git add -A`.

## Next bounded task
Get repository-layout PR #56 through CI and review, then merge it. Afterward,
choose whether to seed missing `high_error_rate` skills deterministically at
$0 or through paid train investigations, and whether the 8-trial holdout
`full` versus `no_memory` comparison merits about $8. No paid run is authorized.
