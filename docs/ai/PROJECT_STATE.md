# PROJECT_STATE.md

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible, production-operable SRE
agent. Deterministic policy and durable state—not model prose—must control every
write, approval, status transition, and operator-facing claim.

## Current milestone
P0 #4 crash-resumable live remediation is implemented and live-verified. ACT now
hands an exact action batch to a dedicated Temporal `LiveRemediationWorkflow`,
which checkpoints one action activity at a time. A real worker-death/alert-clear
run proved the completed action was not replayed and Task #40 still suppresses
all remaining remediation after external recovery. Langfuse’s generic nested
`agent` observation names are replaced with stable specialist-role names and
verified on a fresh production trace.

## Current architecture and invariants
- `act_phase.build_live_action_requests()` is the shared serialization boundary.
  `execute_live_action_request()` executes one request through
  `mutation_gateway.authorize_and_execute()`, the sole fresh authorization,
  tenant/namespace, incident-state, idempotency, and audit boundary.
- `LiveRemediationWorkflow` is infrastructure ACT only. It starts or joins the
  deterministic `incident-live-remediation-<incident>-<action-hash>` ID and
  schedules actions sequentially. `IncidentRemediationWorkflow` remains the
  separate code-fix/sandbox/PR state machine.
- Temporal activity completion is the replay boundary. A replacement worker
  receives already-completed results from history; it never calls the completed
  activity again.
- Task #40 is load-bearing: Alertmanager clear marks the incident resolved,
  expires approvals, and withdraws write authority. The next activity re-reads
  that durable state, returns `REFUSED/incident_resolved`, stops scheduling, and
  skips verification while preserving completed results in Slack/timeline text.
- Human Slack approval cannot override hard policy blocks. `EXECUTOR_LIVE=true`
  fails closed without Temporal and an incident ID.
- Langfuse graph node keys remain unchanged. A role-aware callback only renames
  the exact internal LangGraph chain name `agent` to `<role>_reasoning`, using a
  code-owned low-cardinality metadata key. Trace identity remains stable:
  `investigate-incident`, incident session ID, environment, cluster/service tags.
- Run-manifest `root_trace_id` is Sentinel’s internal trace-evidence correlation
  ID. The Langfuse public trace ID may differ; incident session and manifest/root
  IDs remain trace metadata for pivots.

## Completed or verified work
- Controlled incident `b72227ab-63bc-4632-a44d-598feec3f237`, workflow
  `incident-live-remediation-b72227ab-63bc-4632-a44d-598feec3f237-a4bd1ecff20b`:
  action 0 scaled `inventory-service` to its existing one replica and reached
  `CHECKPOINTED_ACTION_0`; the Temporal worker was stopped; Alertmanager resolved
  the source alert; the incident became `resolved` and its pending approval
  expired; the replacement worker completed with one `EXECUTED`, one
  `REFUSED(incident_resolved)`, phase `SUPPRESSED_ALERT_CLEARED`, and no
  verification. Temporal status was `COMPLETED` (history length 24).
- Canonical audit contained exactly one `SCALE:EXECUTED`. Deployment replicas
  remained 1 and the pod UID/start time stayed unchanged, proving no replay or
  later scale/restart. Slack/timeline preserved the completed action and stated
  that remaining writes were suppressed.
- Fresh Langfuse incident `e35e6e3e-c820-4057-a2b5-96e131461d2c`, trace
  `1814f33e5e50a4aaf93e788ebcaba7d4`: 142 observations; `agent` count 0;
  role names include all five `<role>_reasoning` specialists; 0 observations
  missing both input/output; 0 errors; all 31 generations have model and usage;
  one `investigate-incident` AGENT root. The synthetic incident was resolved.
- Live evidence exposed and fixed misleading text: when an earlier mutation
  succeeded but the alert later cleared, ACT now says verification was skipped
  after the clear, not “nothing mutating executed.”

## Active problem
The live Temporal worker image was stale relative to the API/repository. The
first probe failed importing `NON_MUTATING_ACTIONS`; four drifted dependencies
(`executor.py`, `policy_gate.py`, `execution_context.py`, `namespace_scope.py`)
were synchronized and the retry passed. The running Codespace is correct, but
manual `docker cp` is not a durable deployment strategy.

## Relevant files
- `sre_agent/act_phase.py`
- `sre_agent/incident_remediation_workflow.py`
- `sre_agent/temporal_client.py`
- `sre_agent/sandbox_worker.py`
- `sre_agent/graph_builder.py`
- `sre_agent/resolution_report.py`
- `sre_agent/tracing.py`
- `sre_agent/agent_nodes.py`
- `tests/test_live_remediation_temporal_workflow.py`
- `tests/test_act_integration.py`
- `tests/test_tracing.py`
- `tests/test_agent_observation_names.py`

## Verification commands and latest results
- Focused suite: `.venv/bin/pytest -q tests/test_act_integration.py
  tests/test_resolution_report.py tests/test_temporal_client.py
  tests/test_tracing.py tests/test_agent_observation_names.py
  tests/test_live_remediation_temporal_workflow.py` → **95 passed**.
- Live API and Temporal worker: both healthy after final restart.
- Fresh Langfuse API audit and live Temporal/Kubernetes/PostgreSQL evidence are
  summarized above.

## Known blockers or risks
- Codespace k3s frequently stops after sleep. Run `scripts/codespace_boot.sh`
  and verify `kubectl get nodes` before live actions; the script also repairs the
  Alertmanager webhook after node-IP changes.
- An alert that expires entirely while both k3s/Alertmanager and the API are
  unavailable can miss its resolved webhook and leave an incident parked. The
  synthetic trace incident required same-fingerprint re-fire/clear cleanup.
- `.env.local-backup-20260910` is untracked, contains live secrets, and is not
  ignored. Never stage it; use explicit paths rather than `git add -A`.
- Ordinary transient tool failures still return terminal `ERROR`; only process
  death has durable automatic resumption.

## Next bounded task
Make API/Temporal-worker deployment parity durable: rebuild both services from
one source revision and add a startup/deploy preflight that verifies a shared
code/version fingerprint plus imports required by `LiveRemediationWorkflow`.
Then rerun the focused suite; no additional live mutation is needed.
