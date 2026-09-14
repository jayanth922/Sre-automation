# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a production-grade, resume-flagship SRE agent platform, not an "educational
subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) closed end to end: a real SLO
breach detected, investigated, approved in Slack, remediated with a
value-changing cluster write, verified, and learned from in one unattended
run. Focus is now **Slack-only communication robustness** (standing rule:
"Slack is the only method of all types of communication, so it should be
robust") and live-fire testing of varied incident types (Tasks #4/#5).

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` gating
`execute_autonomous_live()`. Slack is the sole approval channel by design.
Tracing is never load-bearing — every Langfuse path degrades to "run
untraced". See `docs/ai/DECISIONS.md`.

**Four dispatch families in `executor.py`**: `EXECUTOR_TOOL_MAP` (infra MCP),
`GITHUB_EXEC_TOOL_MAP` (code MCP), `NOTIFY_ONLY_ACTIONS` (reaches a human,
mutates nothing), `READ_ONLY_ACTIONS` (reaches the cluster, reads only). Any
"is this a known action?" check must consult all four. `NON_MUTATING_ACTIONS`
= notify-only ∪ read-only is what verification and skill learning exclude, so
a page is never graded a fix.

**Dispatch routes on capability, not on the action's name.**
`executor.live_tool_for_action()` (one argument) is the single answer to "can
Sentinel really execute this?". `patch`/`config_change` resolve *from their
parameters*: `patch_resource_limits` with a cpu/memory limit,
`patch_deployment_env` with `parameters.env`, nothing otherwise
(ConfigMap/Helm/prose → capability gap). Consulted at plan (`act_phase`),
authorization (`mutation_gateway`) and dispatch (`executor`). **`code_fix`
reaches no map at all** and always reports `SKIPPED` — every code-level root
cause ends with a human writing the fix. Surfaced honestly, not solved.

**Authorization never comes from model-authored text.** In every agent prompt
and enforced by `prompt_guard` — yet `policy_engine` Rule 4 violated it for as
long as it existed. Audit any new gate: the flag must come from deterministic
runtime state, and a hard `(bool, reason)` block is unappealable by design —
if a human should be able to say yes, the hold belongs in
`policy_gate.decide`.

**Env writes are guarded at the edge**, not in the agent: `patch_deployment_env`
read-modify-writes (a k8s strategic merge on `env` replaces the whole list)
and refuses credential-named keys and `valueFrom`.

**Only a human's Slack `acknowledge` resolves an incident.**
`compute_incident_status` never returns RESOLVED — a verified fix stops at
`PENDING_ACKNOWLEDGMENT`.

**`incident_reconciler` is the clock this system otherwise lacks.** A state
nobody polls is a state nobody is told about. `REMEDIATION_IN_PROGRESS` is
*transient*: stranded incidents sweep to `VERIFICATION_UNKNOWN`, deliberately
without replaying a possibly non-idempotent write — the safety net for
**post-approval remediation having no durable job** (`decide_action_approval`
drives `graph.astream` in the caller's process; no lease, no retry). An
approval deadline is a clock too: every other `EXPIRED` write is reactive, so
a lapsed offer is retired only here.

**An active incident is either *worked* or *parked*.** `OPEN`,
`INVESTIGATING`, `REMEDIATION_IN_PROGRESS` have work in flight and
`AWAITING_APPROVAL` has a question in front of a human — dedup there is
rightly silent. `INVESTIGATED`, `REMEDIATION_FAILED`, `VERIFICATION_UNKNOWN`,
`PENDING_ACKNOWLEDGMENT` are parked: nothing runs, nobody was asked, no future
event moves them, so a re-firing alert dies in the dedup branch.
`alerts._PARKED_INCIDENT_STATUSES` posts one notice an hour there. **Nothing
re-runs an investigation** — no war-room command does it and
`/incidents/trigger` dedups on the same title — so `mark resolved` (close it,
let the next alert open a fresh one) is the only way forward, and is what both
the parked and lapse notices say.

**Every node that emits a namespace must be told the cluster's namespace**
(`act_phase` hard-blocks actions outside it), and **an Alertmanager group is
one payload with many members** that may mix firing and resolved, so a
condition clears only when no member of the same payload still reports it
firing.

## Completed or verified work
Twenty-two defects found and fixed by live fire. The recurring pattern, and
the thing to keep testing for: **the system computes the truth, records it,
and then does not tell the human.** Per-defect detail is in git log. Three
corollaries to apply to anything new:
- **Check the sweep, not just the handler** (#19). The late-`approve fix`
  handler was correct throughout, so every test passed while the DB lied for
  hours. Of any deadline stated in Slack, ask: what fires then?
- **Check where an alert goes when its incident is already open** (#22).
  Dedup is the one path with no investigation behind it.
- **Check that a message names a command the system accepts** (#22). Two
  notices offered "re-run the investigation"; nothing does that.

## Active problem
None blocking. #18 (`policy_gate` rollback floor) is deployed but unseen live
— no run has proposed a production rollback since. #20 (planner must
`escalate` rather than propose a `valueFrom` `config_change`) and #21
(narrative must not restate a stale alert annotation as fact) are deployed and
tested but not yet live-exercised; both need the fresh memory-leak
investigation blocked below.

## Relevant files
`sre_agent/`: `executor.py`, `act_phase.py`, `approval_flow.py` (the Slack
message that gates everything), `policy_gate.py`, `incident_reconciler.py`,
`api/v1/alerts.py` (dedup + parked-incident notice), `graph_builder.py`
(planner/swarm prompts), `narrative.py` (the human-facing TL;DR),
`resolution_report.py`; plus
`edge_mcp_servers/mcp_servers/executor_real/server.py` (edge guardrails).

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1164 passed,
  3 skipped**.
- **Clean end-to-end live run, `dc1712ca`**: 7 firing series → one incident →
  correct root cause → Slack `approve fix` → `1/1 mutating action EXECUTED`
  (a real `config_change` → `patch_deployment_env`) → `Verification: RESOLVED
  (alert no longer firing after 330s)` → generative runbook →
  `pending_acknowledgment`.
- **Parked re-fire notice, live**: `d3ca5138` and `f8ca9a54` each got exactly
  one notice (11:19:00Z / 11:14:38Z) with an `alert_refired` timeline row; the
  11:20:00Z redelivery was suppressed by the cooldown; dedup still created
  zero incidents.
- **Lapsed-approval sweep, live**: retired six stale `pending` rows, moved
  `d3ca5138` to `investigated`, posted to the real thread; `approved` rows
  untouched.
- **Edge guardrails, live**: `DATABASE_PASSWORD`, `kube-system`, `valueFrom`
  and prose-only `config_change` all refused.

## Known blockers or risks
- **Live mutation surface includes arbitrary env vars** in an allow-listed
  namespace; the edge denylist is the only thing between a prompt-injected
  credential write and the cluster. `EXECUTOR_ALLOWED_ENV_KEYS` is unset here.
- **Severity is always `UNKNOWN` for real Meridian alerts** (rules emit no
  impact/urgency annotations), so the autonomous path never engages.
- **Stale alert rules** (they live only in the live `prometheus-config`
  ConfigMap, not in this repo): `InventoryMemoryApproachingLimit` fires above
  `1e6` bytes while claiming 200MB; checkout's real limit is `768Mi`, not the
  `256Mi` its description asserts.
- **Runbook ownership gap**: `#checkout-oncall` owns an inventory runbook.
- Known app bug: `checkout-service app.py:147` `int(order_id[-1])` raises
  ValueError on non-numeric order ids — the real cause of
  `CheckoutHighErrorRate`, correctly root-caused and not agent-fixable.
- After a restart no `forward_events` task exists for previously-open
  incidents; `post_to_incident_thread` is the restart-safe path.
- `.env.local-backup-20260910` (untracked) holds live secrets and is NOT
  matched by `.gitignore`'s `.env` pattern — never commit it; use explicit
  paths in `git add`.

## Operating the Codespace
Run `scripts/codespace_boot.sh` after any resume — nothing else starts k3s,
and it self-heals the Alertmanager webhook when the node IP changes. The API
container is image-baked with no source mount: each change needs base64 →
`docker cp` into `/app` → md5 → `py_compile` → `docker restart sre-agent-api`,
one file at a time. Scripts run as `docker exec -w /app sre-agent-api uv run
python …`; complex SQL is piped into `docker exec -i sre-postgres psql` via
stdin. Incident `status` values are lowercase;
`incident_timeline_events.payload_json` is **text**, so cast
`(payload_json::jsonb)`. `clusters` has no `organization_id` — get it from
`approval_requests`. The only namespace is `meridian`. Service ClusterIPs are
reachable from the host; `kubectl port-forward` is not (no `socat` on the
node). Free tier is core-hour capped.

## Next bounded task
Task #4 classes exercised: slow-query (`dc1712ca`), dependency-down
(`8c925dbd`, `b5287f11`), client-side/false-positive (`2c49ac9d` — the agent
correctly proposed **zero** mutations), unhandled application exception
(`f8ca9a54`), memory-leak (`d3ca5138`, investigation only — window lapsed).

**Blocked on a human in Slack.** `d3ca5138` is parked at `investigated` and
dedup folds every re-firing memory alert into it, so no fresh investigation
can start. Reply `mark resolved` on its thread; the next delivery (~1 min)
opens a new incident, and a prompt `approve fix` on that one exercises #20
(the planner should `escalate` about ConfigMap `meridian-config`, not propose
a `config_change` on the `valueFrom`-sourced `CHAOS_MODE`), #21 (the TL;DR
must not repeat the rule's stale "256Mi / OOMKill is imminent" prose over its
own measurement against the live 768Mi limit), and restart-as-band-aid
honesty. The leak runs at the app default 1 KB/request, ~236 MB simulated
against a 200 MB threshold — firing, no OOM risk; tune it with
`POST http://<checkout clusterIP>:8001/admin/config {"leak_kb_per_request": N}`.

Then Task #5 (hardware-type incidents). One real in-thread Slack *question*
remains the last unexercised link of the follow-up path.
