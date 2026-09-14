# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) complete and live-fire closed end
to end: a real SLO breach is detected, investigated, approved in Slack,
remediated with a value-changing cluster write, objectively verified, and
learned from in one unattended run. Focus is now **Slack-only communication
robustness** (standing rule: "Slack is the only method of all types of
communication, so it should be robust") and live-fire testing of varied
incident types (Tasks #4/#5).

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` gating
`execute_autonomous_live()`. Slack is the sole approval channel by design.
Tracing is never load-bearing: every Langfuse path degrades to "run untraced"
rather than raising. See `docs/ai/DECISIONS.md`.

**Four dispatch families in `executor.py`**: `EXECUTOR_TOOL_MAP` (infra MCP),
`GITHUB_EXEC_TOOL_MAP` (code-change MCP), `NOTIFY_ONLY_ACTIONS` (reaches a
human, mutates nothing), `READ_ONLY_ACTIONS` (reaches the cluster, reads
only). Any "is this a known action?" check must consult all four.
`NON_MUTATING_ACTIONS` = notify-only ∪ read-only is what verification and
skill learning must exclude, so a page is never graded a fix.
`approval_effect()` classifies one action for the human-facing approval text.

**Dispatch routes on capability, not on the action's name.**
`executor.live_tool_for_action()` is the single answer to "can Sentinel really
execute this?". `patch`/`config_change` resolve *from their parameters*: to
`patch_resource_limits` with a cpu/memory limit, to `patch_deployment_env`
with `parameters.env`, to nothing otherwise (ConfigMap/Helm/prose → capability
gap). Consulted at plan (`act_phase`), authorization (`mutation_gateway`) and
dispatch (`executor`). **`code_fix` reaches no map at all**, so it always
reports `SKIPPED` — every code-level root cause ends with a human writing the
fix. Surfaced honestly, not solved.

**Env writes are guarded at the edge.** `patch_deployment_env`
read-modify-writes (a k8s strategic merge on `env` replaces the whole list),
refuses credential-named keys and `valueFrom`, caps key count/value length,
honours `EXECUTOR_ALLOWED_ENV_KEYS`, returns exact `prior_env`.

**Only a human's Slack "acknowledge" resolves an incident.**
`compute_incident_status` never returns RESOLVED — a verified fix stops at
`PENDING_ACKNOWLEDGMENT` — and `resolved_at_for_status` stamps `resolved_at`
only for RESOLVED. `REMEDIATION_IN_PROGRESS` is *transient*; the
`incident_reconciler` sweeps incidents stranded in it to
`VERIFICATION_UNKNOWN` and posts to Slack, deliberately without replaying the
remediation. It is the safety net for the fact that **post-approval
remediation has no durable job**: `decide_action_approval` drives
`graph.astream` synchronously in the caller's process, with no lease or retry.

**Every node that emits a namespace must be told the cluster's namespace.**
`act_phase` hard-blocks actions outside it; the investigation swarm and the
planner (`planner_namespace_scope`) both receive it explicitly.

**An Alertmanager group is one payload with many members** that may mix firing
and resolved, so `alerts.py` clears a condition only when no member of the
same payload still reports it firing.

## Completed or verified work
Seventeen defects found and fixed by live fire. The recurring pattern, and the
thing to keep testing for: **the system computes the truth, records it, and
then does not tell the human.** Learning demanded a status the graph cannot
produce; a stranded remediation stayed silent for 27h; the approval prompt
counted notifications and unexecutable actions as cluster writes; the
resolution report dropped the recorded reason a patch was missing. Per-defect
detail is in git log, not here.

## Active problem
Defects #15 (`approval_effect`) and #16 (`planner_namespace_scope`) are
committed and deployed but not yet live-confirmed — every previously open
incident's approval message predates them. Incident `d3ca5138`
(`CheckoutMemoryApproachingLimit`, 07:12 UTC) is the confirmation vehicle.

## Relevant files
`sre_agent/`: `executor.py` (dispatch maps, `approval_effect`), `act_phase.py`
(gates, namespace block), `approval_flow.py` (the Slack message that gates
everything), `graph_builder.py` (planner/swarm prompts),
`resolution_report.py`, `incident_reconciler.py`; plus
`edge_mcp_servers/mcp_servers/executor_real/server.py` (edge guardrails).

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1114 passed,
  3 skipped**.
- **Clean end-to-end live run, `dc1712ca`**: 7 firing series → exactly one
  incident → correct root cause → Slack `approve fix` → `1/1 mutating action
  EXECUTED` (value-changing env write) → `Verification: RESOLVED (alert no
  longer firing after 330s)` → generative runbook → `pending_acknowledgment`.
- Edge guardrails verified live: `DATABASE_PASSWORD` refused with `[REDACTED]`
  recorded, `kube-system` refused, `valueFrom` refused, prose-only
  `config_change` rejected `unsupported_action`.

## Known blockers or risks
- **Live mutation surface includes arbitrary env vars** in an allow-listed
  namespace; the edge denylist is the only thing between a prompt-injected
  credential write and the cluster. `EXECUTOR_ALLOWED_ENV_KEYS` is unset here.
- **Severity is always `UNKNOWN` for real Meridian alerts** (rules emit no
  impact/urgency annotations), so the autonomous path never engages.
- **`rollback` was blocked by PolicyEngine** with "Requires explicit approval
  flag" on PROD *even after* a human approved in Slack. Not yet chased.
- **Alert rule bug**: `InventoryMemoryApproachingLimit` fires above `1e6`
  bytes while its description says 200MB; `checkout-service`'s real limit is
  now `768Mi`, so both memory descriptions are stale.
- **Runbook ownership gap**: `#checkout-oncall` owns an inventory runbook.
- Known app bug: `checkout-service app.py:147` `int(order_id[-1])` raises
  ValueError on non-numeric order ids — the real cause of
  `CheckoutHighErrorRate`, correctly root-caused (commit `7a6e223a`) and not
  fixable by the agent.
- After a restart no `forward_events` task exists for previously-open
  incidents; `post_to_incident_thread` is the restart-safe path.
- `.env.local-backup-20260910` (untracked) holds live secrets and is NOT
  matched by `.gitignore`'s `.env` pattern — never commit it; use explicit
  paths in `git add`.

## Operating the Codespace
The API container is image-baked with no source mount: each change needs
base64 → `docker cp` into `/app` → md5 → `py_compile` → `docker restart
sre-agent-api`, one file at a time. Scripts run as `docker exec -w /app
sre-agent-api uv run python …`; complex SQL is piped into `docker exec -i
sre-postgres psql` via stdin. Incident `status` values are lowercase;
`incident_timeline_events` stores `payload_json`. The only cluster namespace
is `meridian` (`demo-app` does not exist). Free tier is core-hour capped.

## Next bounded task
Finish `d3ca5138` and confirm #15/#16 live. Task #4 classes already exercised:
slow-query (`dc1712ca`), dependency-down (`8c925dbd`, `b5287f11`),
client-side/false-positive (`2c49ac9d` — the agent correctly proposed **zero**
mutations), unhandled application exception (`f8ca9a54`). The memory-leak run
is the first to exercise the *executable* `config_change` path and the first
where a restart is only a band-aid, so it is also a direct honesty test.
Then Task #5 (hardware-type incidents). One real in-thread Slack *question*
remains the last unexercised link of the follow-up path.
