# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a production-grade, resume-flagship SRE agent platform, not an "educational
subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) closed end to end once:
`dc1712ca` went breach → investigation → Slack `approve fix` → real cluster
write → verified → learned, unattended. Focus is **Slack-only communication
robustness** (standing rule: "Slack is the only method of all types of
communication, so it should be robust") and live fire on varied incident
types (Tasks #4/#5).

## Current architecture and invariants
Two independent ACT gates (`PolicyEngine.evaluate_action()`,
`policy_gate.decide()`), plus `EXECUTOR_LIVE` gating
`execute_autonomous_live()`. Slack is the sole approval channel by design.
Tracing is never load-bearing. See `docs/ai/DECISIONS.md`.

**Four dispatch families in `executor.py`**: `EXECUTOR_TOOL_MAP` (infra MCP),
`GITHUB_EXEC_TOOL_MAP` (code MCP), `NOTIFY_ONLY_ACTIONS` (reaches a human,
mutates nothing), `READ_ONLY_ACTIONS` (reads the cluster). Any "is this a
known action?" check must consult all four. `NON_MUTATING_ACTIONS` is what
verification and skill learning exclude, so a page is never graded a fix.

**Dispatch routes on capability, not on the action's name.**
`executor.live_tool_for_action()` is the single answer to "can Sentinel really
execute this?". `patch`/`config_change` resolve *from their parameters*:
`patch_resource_limits` for cpu/memory, `patch_deployment_env` for
`parameters.env`, nothing otherwise (ConfigMap/Helm/prose → capability gap).
**`code_fix` reaches no map** and always reports `SKIPPED`.

**Authorization never comes from model-authored text** — enforced by
`prompt_guard`, and the flag must come from deterministic runtime state. A
hard `(bool, reason)` block is unappealable; if a human should be able to say
yes, the hold belongs in `policy_gate.decide`. Note that gate decides
*autonomy vs. approval only* — it never chooses the action.

**Only a human's Slack `acknowledge` resolves an incident.**
`compute_incident_status` never returns RESOLVED.

**`incident_reconciler` is the clock this system otherwise lacks**, because
post-approval remediation has no durable job: `decide_action_approval` drives
`graph.astream` in the caller's process. Stranded incidents sweep to
`VERIFICATION_UNKNOWN` without replaying a possibly non-idempotent write, and
lapsed approvals are retired only here.

**An active incident is either *worked* or *parked*.** Parked
(`INVESTIGATED`, `REMEDIATION_FAILED`, `VERIFICATION_UNKNOWN`,
`PENDING_ACKNOWLEDGMENT`) means nothing runs and no future event moves it, so
a re-firing alert dies in dedup. **Nothing re-runs an investigation** — `mark
resolved` and let the next alert open a fresh one is the only way forward.

**A specialist repeating the alert's prose is not a second source.** Prompt
rules do not hold here (8/8 real-model replays defeated them), so
`narrative._echoed_alert_claims` appends a deterministic "Carried over from
the alert text, not measured" footer. **An honesty fix that only edits a
prompt is unfinished** — put the correction below the model's text.

**Structured output is not structurally trustworthy.** Function-calling models
stringify nested containers; `agent_state._decode_json_container` decodes them
`mode="before"` on every LLM-filled list/dict field. Without it a
ValidationError silently became a fallback plan (see below).

## Completed or verified work
Twenty-six defects found by live fire. The recurring pattern, and the thing to
keep testing for: **the system computes the truth, records it, and then does
not tell the human.** Per-defect detail is in git log. Corollaries:
- **Check the sweep, not just the handler** (#19). Of any deadline stated in
  Slack, ask: what fires then?
- **Check where an alert goes when its incident is already open** (#22).
- **Check that a message names a command the system accepts** (#22).
- **Replay real evidence against the real model before believing a prompt
  fix** (#23).
- **A fallback path is a claim about the world** (#26). Every `except` that
  substitutes a default must say it did.

Recent, all committed, none pushed:
- **#25 — the retry machinery was disabled by identity-map aliasing.**
  `fail_job()`'s `select()` returns the caller's own object, so checking
  `job_row.lease_owner` afterwards read `fail_job`'s own writes and the
  fallback overwrote the PENDING it had just set. Three investigations died at
  `attempt_count=1` of 3 and none retried. Only the return value distinguishes.
- **#25b — an investigation that died told nobody.** Its Slack thread said
  "Incident opened" and then nothing, forever. Terminal failures now post.
- **#26 — no remediation plan had ever survived parsing.** 4/4 planner runs
  failed `1 validation error for RemediationPlan / actions / Input should be a
  valid list`: the model sent `actions` as a JSON string. `_planner_node`'s
  except branch then substituted one hard-coded `escalate manual_review`,
  which Slack rendered as a real "Proposed plan" reasoned with the policy
  gate's unrelated "unknown or incomplete telemetry". **This is why
  `patch_resource_limits` had never been exercised** — good plans were being
  discarded before the gate, the executor, or a human saw them.
- **Observability repairs** (live, not in git): promtail now uses k8s pod
  discovery instead of a hand-maintained 8-entry allow-list (13/13 pods;
  `payment-service` collected for the first time; the `pod` label the Loki
  Specialist queries on now exists). Two traps: promtail auto-injects
  `spec.nodeName=$HOSTNAME`, and in a pod `HOSTNAME` is the *pod* name, so it
  matches zero pods **with no error anywhere** — fix is the downward-API env
  var. And this k3s runs `--docker`, so the pipeline needs `docker: {}`, not
  `cri: {}`, which fails silently and stores the raw envelope. Also added
  kube-state-metrics + a `cluster-resources` rule group, and gave
  `PodOOMKilled` a recency guard (`kube_pod_..._last_terminated_reason` has no
  recency and fires forever otherwise).

## Active problem
**Task #5 is one Slack reply from done.** With #26 deployed, incident
`f0cca59f` produced the platform's first real plan — 6 actions, not the
fallback — including `config_change` on `deployment/thumb-worker` with
`parameters {namespace: meridian, container: thumb-worker, memory: 256Mi}`.
That resolves to `patch_resource_limits`, and the approval message correctly
reads "1 change to the cluster". **It needs a human to reply `approve fix`**
in that thread; the 30-minute offer lapses and `incident_reconciler` retires
it, after which a fresh alert is needed. Approving is what finally exercises
`patch_resource_limits` end to end.

**Narration fidelity, unfixed.** On `4a0b0254` the supervisor TL;DR claimed "a
regression introduced in that rollout" and "the Loki Specialist found no error
logs", discarding the specialist's actual finding (a 150 MiB warm-up cannot
fit a 64Mi limit). Same family as #21/#23/#24: for an on-call reading only the
TL;DR, that is a materially wrong root cause.

## Relevant files
`sre_agent/`: `agent_state.py` (LLM-facing schemas + container decoding),
`graph_builder.py` (planner/swarm prompts, fallback plan), `act_phase.py`,
`approval_flow.py` (the Slack message that gates everything), `executor.py`,
`policy_gate.py`, `incident_reconciler.py`, `api/v1/alerts.py` (dedup),
`narrative.py`, `job_store.py`, `war_room_service.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1202 passed,
  3 skipped**.
- Health endpoint is `/ping` on **port 8080** (`/health` 404s).
- **Promtail fix verified to improve diagnosis, live**: on `4a0b0254` the Loki
  Specialist reported "6 restarts each dying right after logging 'warming 150
  MiB page cache', exponential backoff 13s→24s→47s→96s→166s" — logs that did
  not reach Loki before the fix.
- **#25/#25b are unit-tested only** (`tests/test_investigation_failure_path.py`);
  credits were restored before another failure could be forced live.
- `dc1712ca` remains the reference clean run (real `patch_deployment_env`,
  `RESOLVED after 330s`, generative runbook).

## Known blockers or risks
- **Severity is always `UNKNOWN` for real Meridian alerts** (rules emit no
  impact/urgency annotations), so the autonomous path never engages. Whether
  this is a missing-annotation problem in the locally-owned `cluster-resources`
  rules or a platform defect is still unanswered.
- **Live mutation surface includes arbitrary env vars** in an allow-listed
  namespace; the edge denylist is the only guard. `EXECUTOR_ALLOWED_ENV_KEYS`
  is unset.
- **Alert rules live only in the live `prometheus-config` ConfigMap, not in
  this repo.** Several are stale (`InventoryMemoryApproachingLimit` fires above
  `1e6` bytes while claiming 200MB; checkout's real limit is `768Mi`, not the
  `256Mi` its description asserts).
- **`ANTHROPIC_MODEL=claude-3-5-sonnet-latest` in the API container 404s**, so
  any `create_llm_with_fallback()` default path runs silently degraded. The
  agent path routes via `ExecutionContext`/`route_llm` and is unaffected.
- Known app bug: `checkout-service app.py:147` `int(order_id[-1])` — the real
  cause of `CheckoutHighErrorRate`, correctly root-caused, not agent-fixable.
- `.env.local-backup-20260910` (untracked) holds live secrets and is **not**
  matched by `.gitignore`'s `.env` pattern — never commit it; always use
  explicit paths in `git add`.
- **Nothing has been pushed.** Local-only: `416c84f`, `936eefc`, `0eb6ee5`,
  `50815bd`, `fef1a14`.

## Operating the Codespace
Run `scripts/codespace_boot.sh` after any resume — nothing else starts k3s,
and it self-heals the Alertmanager webhook when the node IP changes. The API
container is image-baked with no source mount: each change needs base64 →
`docker cp` into `/app` → md5 → `py_compile` → `docker restart sre-agent-api`.
**Deploy with `deploy.sh`-style per-chunk `>` writes, never `>>`** — the ssh
wrapper retries, and a retried append silently duplicated a payload 4×.
Scripts run as `docker exec -w /app sre-agent-api uv run python …`; SQL pipes
into `docker exec -i sre-postgres psql`. Incident `status` values are
lowercase; `incident_timeline_events.payload_json` is **text**, so cast
`(payload_json::jsonb)`. `clusters` has no `organization_id`;
`approval_requests` has no `action_type`. The only namespace is `meridian`.
Service ClusterIPs are reachable from the host; `kubectl port-forward` is not.

## Next bounded task
Finish Task #5: confirm the planner now proposes `config_change` with
`parameters.memory` on `ocr-extractor`, approve it in Slack, and watch
`patch_resource_limits` make a real cluster write.

Task #4 classes already exercised: slow-query (`dc1712ca`), dependency-down
(`8c925dbd`, `b5287f11`), client-side false-positive (`2c49ac9d` — correctly
proposed **zero** mutations), unhandled exception (`f8ca9a54`), memory-leak
(`d3ca5138`, investigation only). Task #4 is **blocked on a human** replying
`mark resolved` on `d3ca5138`'s Slack thread; the next delivery opens a fresh
incident that would exercise #20 (`escalate` about ConfigMap `meridian-config`
rather than a `config_change` on the `valueFrom`-sourced `CHAOS_MODE`).

One real in-thread Slack *question* remains the last unexercised link of the
follow-up path.

Cluster cleanup owed: `thumb-worker`, `pdf-thumbnailer`, `ocr-extractor` are
crashlooping by design; incidents `4a0b0254`, `f0cca59f`, `3b879513`,
`47560f97` are open.
