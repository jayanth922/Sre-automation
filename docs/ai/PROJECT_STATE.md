# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.
Deterministic policy and durable state—not model prose—must control writes,
approvals, status transitions, and operator-facing claims.

## Current milestone
P0 #4 crash-resumable live remediation is deployed and live-verified. Temporal
checkpoints each action; a replacement-worker run proved successful writes are
not replayed and Task #40 still withdraws later remediation authority after an
external clear. Stable Langfuse specialist names, fail-closed API/worker image
parity and missed-clear reconciliation are also deployed.
Live-action transport failures are now classified at the idempotency boundary:
only proven pre-claim failures receive bounded Temporal retries, while
claim/dispatch/audit uncertainty stops the plan for manual review. This complete
runtime is deployed from exact revision `d6a6a1b`.
The P1 reflector branch is now a deployed bounded, allowlisted graph loop.

## Current architecture and invariants
- `act_phase.build_live_action_requests()` serializes the exact approved batch.
  `LiveRemediationWorkflow` schedules one activity at a time; the separate
  `IncidentRemediationWorkflow` continues to own code-fix/sandbox/PR work.
- `mutation_gateway.authorize_and_execute()` remains the sole fresh incident,
  policy, tenant/namespace, idempotency, and audit boundary.
- Unexpected failures before an idempotency claim become retryable
  `MutationPreDispatchError`; once the claim starts, failure is retry-unsafe.
  Temporal makes at most three attempts. An `ERROR`/unknown outcome is terminal
  for the batch, and MCP-client teardown cannot replace a successful result.
- A replacement worker consumes completed activity results from Temporal
  history. After Alertmanager clear, the next activity returns
  `REFUSED/incident_resolved`, stops scheduling, and skips verification while
  retaining completed results in Slack/timeline output.
- Human approval cannot override hard policy blocks. `EXECUTOR_LIVE=true` fails
  closed without Temporal and an incident ID.
- Langfuse graph keys are unchanged. Only the exact internal chain name `agent`
  becomes `<role>_reasoning` from code-owned, low-cardinality metadata.
- Lost resolved webhooks are recovered only when the original durable alert job
  identifies a Prometheus rule that exists and is healthy, and two snapshots at
  least five minutes apart show no matching active series. The first observation
  is durable; missing/unhealthy/unreachable source state leaves the incident open.
- API and Temporal worker share one image and verify the same source manifest
  before work; deployment fails if their runtime identities differ.

## Completed or verified work
- A controlled workflow checkpointed action 0, lost its worker, then resumed
  after alert clear with one `EXECUTED`, one `REFUSED(incident_resolved)`, no
  verification, exactly one audit, and no repeated cluster mutation.
- Fresh Langfuse trace `1814f33e5e50a4aaf93e788ebcaba7d4`: 142 observations,
  zero generic `agent` names, zero errors/missing I/O, and all 31 generations
  carried model and usage metadata.
- A process-restart regression recovers after two healthy absences, invokes
  Task #40 once, records `remediation_verified=false`, and cannot replay closure.
  A CAS prevents late-webhook/reconciler races.
- Live incident `193c3bf3…` lost its resolved webhook while the API/receiver
  were unavailable. Two healthy Prometheus snapshots recovered it exactly once;
  the run recorded `remediation_suppressed.reason=incident_resolved`, zero live
  writes, and zero action/gate approvals.
- Boundary tests prove: one transient pre-dispatch failure yields two activity
  attempts but one external mutation; exhaustion stops after three; an
  ambiguous dispatched outcome runs once and schedules no later action; audit
  and claim uncertainty remain terminal; cleanup failure preserves success.
  The original replacement-worker/post-clear regression still passes.
- Live no-mutation workflow `live-retry-safe-probe-d6a6a1b` used an unmapped
  action and nonexistent cluster. History recorded retry maximum 3, final
  activity attempt 3, `LiveActionPreDispatchError`, and
  `MANUAL_REVIEW_REQUIRED/pre_dispatch_retries_exhausted`. Redis had no claim
  and the executor edge had no matching call. API/worker remained healthy.
- Reflector re-investigation now validates model recommendations against four
  evidence agents, keeps callables outside checkpoint state, reruns only the
  selected agents, increments a durable depth counter, and conditionally loops
  `reflector → investigation_swarm → reflector`. Invalid names and depth
  exhaustion fall through to the planner. Stable observation names preserve a
  readable Langfuse cycle/expanded DAG.

## Active problem
Large evidence and context payloads are retained directly in LangGraph state.
They need mapping against durable artifact storage before selecting the
smallest reference-backed replacement that preserves restart and trace behavior.

## Relevant files
- `sre_agent/act_phase.py`, `sre_agent/incident_remediation_workflow.py`
- `sre_agent/mutation_gateway.py`
- `sre_agent/alert_lifecycle_reconciler.py`, `sre_agent/api/v1/alerts.py`
- `tests/test_live_remediation_temporal_workflow.py`
- `tests/test_live_remediation_activity.py`, `tests/test_mutation_gateway.py`
- `tests/test_alert_lifecycle_reconciler.py`
- `tests/test_deeper_investigation_loop.py`

## Verification commands and latest results
- Focused missed-clear/resolution/reconciler suite: **51 passed**.
- Full suite: **1,488 passed** in the sandbox; its eight Temporal server starts
  were OS-blocked, then all **8 passed** with local-server permission.
- `scripts/check_python_quality.sh`, secret scan, module reachability, Compose
  config, and Helm/Kustomize/Terraform deployment-template gate: passed.
- Reflector-loop focused suite: **37 passed**. Full suite: **1,502 passed**.
- Exact-revision Docker build and live `check_runtime_parity.py`: passed. API
  and worker are healthy on image `f53fec6d…`, revision `202c9f2`, fingerprint
  `ca76813b…`, and 147 files.

## Known blockers or risks
- Codespace k3s often stops after sleep. Run `scripts/codespace_boot.sh` and
  verify `kubectl get nodes` before live actions.
- `.env.local-backup-20260910` is untracked, contains live secrets, and is not
  ignored. Never stage it; avoid `git add -A`.

## Next bounded task
Map large evidence/context payloads retained in LangGraph state against existing
durable artifact storage, then define and implement the smallest artifact-backed
replacement without losing restart safety or Langfuse traceability.
