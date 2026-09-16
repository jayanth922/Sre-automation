# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.
Deterministic policy and durable state—not model prose—must control writes,
approvals, status transitions, and operator-facing claims.

## Current milestone
P0 #4 crash-resumable live remediation is implemented, live-verified, and
deployed. ACT checkpoints each action in Temporal; a real replacement-worker
run proved completed writes are not replayed and Task #40 still withdraws all
remaining remediation authority after an external alert clear. Langfuse’s
generic nested `agent` names are also replaced by stable specialist roles.
API/Temporal-worker image parity is fail-closed and live-confirmed. Missed
Alertmanager resolved notifications now have a fail-closed recovery path.

## Current architecture and invariants
- `act_phase.build_live_action_requests()` serializes the exact approved batch.
  `LiveRemediationWorkflow` schedules one activity at a time; the separate
  `IncidentRemediationWorkflow` continues to own code-fix/sandbox/PR work.
- `mutation_gateway.authorize_and_execute()` remains the sole fresh incident,
  policy, tenant/namespace, idempotency, and audit boundary.
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
- API and Temporal worker are two entrypoints into one `sentinel/api:local`
  image. Docker records a SHA-256 manifest over every Python file in `backend/`
  and `sre_agent/`; both entrypoints verify it before work, the worker also
  imports every registered workflow/activity dependency, and the deploy gate
  compares both running identities.

## Completed or verified work
- Controlled workflow `incident-live-remediation-b72227ab-…-a4bd1ecff20b`:
  action 0 reached `CHECKPOINTED_ACTION_0`; the worker stopped; the source alert
  cleared; the replacement completed with one `EXECUTED`, one
  `REFUSED(incident_resolved)`, phase `SUPPRESSED_ALERT_CLEARED`, and no
  verification. PostgreSQL contained exactly one mutation audit; deployment
  replicas remained 1 and pod identity was unchanged.
- Fresh Langfuse trace `1814f33e5e50a4aaf93e788ebcaba7d4`: 142 observations,
  zero generic `agent` names, zero errors/missing I/O, and all 31 generations
  carried model and usage metadata.
- Commit `373eabe` built once in a clean Codespace checkout and recreated both
  runtimes. Both are healthy on image ID `e075fdbf…`, revision `373eabe`, runtime
  fingerprint `31f7a253…`, and 146 files. Build-time worker imports, both startup
  checks, the worker liveness check, and `check_runtime_parity.py` all passed.
- A process-restart regression persists the first healthy absence, recovers on
  the second, invokes Task #40’s external-clear boundary exactly once, never runs
  human-resolution semantics, records `remediation_verified=false`, and proves a
  later sweep cannot replay closure. A CAS prevents late-webhook/reconciler races.

## Active problem
The missed-clear recovery is locally verified but not yet deployed or exercised
against a real Prometheus rule after deliberately losing its resolved webhook.

## Relevant files
- `sre_agent/runtime_preflight.py`
- `scripts/deploy_agent_runtimes.sh`
- `scripts/check_runtime_parity.py`
- `platform/Dockerfile`, `platform/docker-compose.yaml`
- `sre_agent/act_phase.py`, `sre_agent/incident_remediation_workflow.py`
- `sre_agent/tracing.py`, `sre_agent/agent_nodes.py`
- `sre_agent/alert_lifecycle_reconciler.py`, `sre_agent/api/v1/alerts.py`
- `sre_agent/incident_reconciler.py`, `sre_agent/approval_flow.py`
- `tests/test_runtime_preflight.py`
- `tests/test_live_remediation_temporal_workflow.py`
- `tests/test_alert_lifecycle_reconciler.py`

## Verification commands and latest results
- Focused missed-clear/resolution/reconciler suite: **51 passed**.
- Full suite: **1,481 passed** in the sandbox; its five Temporal server starts
  were OS-blocked, then all **5 passed** with local-server permission.
- `scripts/check_python_quality.sh`, secret scan, module reachability, Compose
  config, and Helm/Kustomize/Terraform deployment-template gate: passed.
- Exact-revision Docker build and live `check_runtime_parity.py`: passed; API
  and worker healthy on the same image ID and fingerprint.

## Known blockers or risks
- Codespace k3s often stops after sleep. Run `scripts/codespace_boot.sh` and
  verify `kubectl get nodes` before live actions.
- `.env.local-backup-20260910` is untracked, contains live secrets, and is not
  ignored. Never stage it; avoid `git add -A`.
- Ordinary transient tool failures remain terminal `ERROR`; process death is
  durable, but business retries are not implemented.

## Next bounded task
Deploy the missed-clear recovery from an exact clean revision, then live-test a
synthetic alert whose resolved webhook is deliberately unavailable. Prove two
healthy rule snapshots close the lifecycle exactly once, preserve
`remediation_verified=false`, and leave no pending approval/write authority.
