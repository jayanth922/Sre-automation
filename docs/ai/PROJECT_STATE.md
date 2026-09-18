# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.
Deterministic policy and durable state—not model prose—must control writes,
approvals, status transitions, and operator-facing claims.

## Current milestone
P0 #4 crash-resumable live remediation is deployed and live-verified. A
replacement-worker run proved successful writes are not replayed and Task #40
still withdraws remediation authority after an external clear. Stable Langfuse
names, fail-closed image parity, missed-clear reconciliation, bounded
pre-claim retries, and reflector/artifact context (`5c292cd`) are deployed.
Observability is deployed (`e2b9fe0`); runtime-only job completion is deployed
from `5c55bed`. Aggregate status grounding is deployed from `50873ae`.

## Current architecture and invariants
- `act_phase` serializes the approved batch; `LiveRemediationWorkflow` runs one
  activity at a time while `IncidentRemediationWorkflow` owns code-fix/PR work.
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
- Lossless specialist tool transcripts and responses are compressed into
  incident-owned, content-addressed PostgreSQL artifacts. Checkpoints keep only
  references, policy measurements, and bounded response context; storage
  failure retains the legacy trace rather than losing evidence.
- The local recorder measures every top-level graph node. Failed calls count as
  runs and latency; Langfuse separately owns model/tool/token/cost semantics.
  No surface claims unobserved cross-provider fallback.
- The canonical SaaS runtime owns the successful durable-job terminal write;
  the queue worker holds the lease and handles only exceptions that escape it.
- Lost resolved webhooks are recovered only when the original durable alert job
  identifies a Prometheus rule that exists and is healthy, and two snapshots at
  least five minutes apart show no matching active series. The first observation
  is durable; missing/unhealthy/unreachable source state leaves the incident open.
- API and Temporal worker share one image and verify the same source manifest
  before work; deployment fails if their runtime identities differ.

## Completed or verified work
- A controlled workflow checkpointed action 0, lost its worker, then resumed
  after alert clear with one `EXECUTED`, one `REFUSED(incident_resolved)`, no
  verification, one audit, and no repeated mutation.
- Fresh Langfuse trace `1814f33e5e50a4aaf93e788ebcaba7d4`: 142 observations,
  zero generic `agent` names, zero errors/missing I/O, and all 31 generations
  carried model and usage metadata.
- A process-restart regression recovers after two healthy absences, invokes
  Task #40 once, records `remediation_verified=false`, and cannot replay closure;
  a CAS prevents late-webhook/reconciler races.
- Reflector re-investigation validates recommendations against four agents,
  keeps callables out of checkpoints, bounds depth, and loops only selected
  agents. Stable names preserve a readable Langfuse cycle/expanded DAG.
- Artifact reload verifies ownership and SHA-256 integrity. Tests prove large
  raw output is absent from successful checkpoints, severity retains tool
  provenance, and storage failure remains lossless. Duplicate findings removed.
- All 14 graph nodes now feed local metrics. Tests pin failed-run denominators,
  complete node coverage, and `fallback_allowed=false`; the dead provider-
  switch dashboard surface was removed. The worker regression test proves it
  does not issue a second completion after the runtime writes its rich result.
- Time-skipping and live Temporal smoke drive both gates, child verification,
  and a mocked PR result. A replacement-worker regression preserves the pending
  gate; denial stops without verification or PR activity. Alert-focused tests
  isolate the settle floor and no longer stall the suite.
- ORM-backed API response models use Pydantic `ConfigDict`; validation
  and serialization remain covered. FastAPI startup/shutdown now run through
  one deployed lifespan context with cleanup regression coverage (`12facc3`).
- Anthropic 1.7.2 now comes from the frozen image graph.
- Qdrant client/server and all manifests are deployed at 1.19.1; a deployment
  contract prevents version drift and reintroduction of `latest`. Temporal's
  deployed dev server/SDK are pinned to CLI 1.8.3 and SDK 1.32.0.

## Active problem
Next: deploy the fully locked runtime and verify exact parity.

## Relevant files
- `backend/schemas.py`, `sre_agent/agent_runtime.py`
- `pyproject.toml`, `platform/docker-compose.yaml`, deployment tests

## Verification commands and latest results
- `scripts/check_python_quality.sh`, secret scan, module reachability, Compose
  config, and Helm/Kustomize/Terraform deployment-template gate: passed.
- Full suite: **1,515 passed** with zero warnings.
- Exact-revision Docker build and live `check_runtime_parity.py`: passed. API
  and worker are healthy on image `3e87403a…`, revision `e78781d`, fingerprint
  `7fbd3205…`, and 149 files. Alembic is at `e5f6a7b8c9d0` (head).

## Known blockers or risks
- Never stage the untracked secret backup `.env.local-backup-20260910`.

## Next bounded task
Build and deploy the frozen Anthropic/Temporal dependency graph, then verify
health and source-manifest parity without external remediation writes.
