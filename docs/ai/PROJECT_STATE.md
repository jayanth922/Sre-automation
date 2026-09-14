# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a production-grade, resume-flagship SRE agent platform, not an "educational
subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) closed end to end on both write
paths: `dc1712ca` via `patch_deployment_env`, and — after #26 — `555a3acb` /
`d2fb7c5d` via `patch_resource_limits` (**Task #5 complete**, see below).
Focus is **Slack-only communication robustness** (standing rule: "Slack is
the only method of all types of communication, so it should be robust") and
Task #4's remaining incident classes.

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

**Authorization never comes from model-authored text** (`prompt_guard`); the
flag must come from deterministic runtime state. A hard `(bool, reason)` block
is unappealable — if a human should be able to say yes, the hold belongs in
`policy_gate.decide`, which decides *autonomy vs. approval only* and never
chooses the action.

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
resolved`, and let the next alert open a fresh one.

**A specialist repeating the alert's prose is not a second source.** Prompt
rules do not hold here (8/8 real-model replays defeated them), so
`narrative._echoed_alert_claims` appends a deterministic "Carried over from
the alert text, not measured" footer. **An honesty fix that only edits a
prompt is unfinished** — put the correction below the model's text.

**Structured output is not structurally trustworthy.** Function-calling models
stringify nested containers; `agent_state._decode_json_container` decodes them
`mode="before"` on every LLM-filled list/dict field. Without it a
ValidationError silently became a fallback plan (see below).

**A verdict needs a floor, not just a deadline.** `verify_alert_cleared`
polls past "still firing" *and* past a too-early "clear":
`min_clear_seconds` (180s) makes a clear reading inside the floor a reason to
keep watching. `ALERTS` is pod-labelled, so every rollout clears it instantly
(#27).

**Resolution is terminal.** Every resolve path — Alertmanager clear, Slack
`acknowledge`, Slack `mark resolved` — goes through
`approval_flow.fire_resolution_side_effects`, whose *first* act is
`job_store.cancel_incident_investigations`. `_run_graph_impl` refuses to start
on an already-resolved incident, before it opens a war room (#28).

## Completed or verified work
Twenty-nine defects found by live fire; per-defect detail is in git log. The
recurring pattern, and the thing to keep testing for: **the system computes
the truth, records it, and then does not tell the human.** Corollaries:
- **Check the sweep, not just the handler** (#19). Of any deadline stated in
  Slack, ask: what fires then?
- **Check where an alert goes when its incident is already open**, and **that
  a message names a command the system accepts** (#22).
- **Replay real evidence against the real model before believing a prompt
  fix** (#23).
- **A fallback path is a claim about the world** (#26). Every `except` that
  substitutes a default must say it did.
- **Ask which direction of an error a safeguard was written for** (#27). The
  poll loop's comment reasoned only about false negatives; the false positive
  sat underneath it untouched.
- **Machinery whose only caller is a manual endpoint has no caller** (#28).
  `request_job_cancel` was complete and correct and nothing on any resolve
  path reached it. Grep for callers, not for the function.
- **A test that passes alone and fails in the suite is usually global state a
  fixture never gave back** (#28). Two leaks, both silent: a `sys.modules`
  stub for `mcp` left installed, and a re-imported `backend.database` — a
  second module object with its own engine, so patching
  `backend.database.AsyncSessionLocal` patched something nothing held. Mutate
  `sys.modules` only through `monkeypatch`, and patch the module object the
  code under test is holding.
- **A status is a label, not evidence** (#29). `open` and `investigating` both
  name an activity; neither proves one is happening, because the failure path
  writes `open` and a dead worker leaves `investigating` behind. Ask the table
  that actually knows (`jobs`), and classify the ambiguous states rather than
  letting them inherit the happy-path reading.
- **Re-read the row before blaming the current build** (#30, withdrawn). Five
  jobs at `status=failed, attempt_count=1 of 3` looked like a live retry
  bypass; `last_error` *and* `result` were both set, which only the pre-#25
  code produces, and the container's `agent_runtime.py` post-dates them.

**#29** (fixed, live-confirmed): an investigation that dies leaves a silent
alert black hole. `3b879513` ([pdf-thumbnailer] PodOOMKilled) lost its run two
seconds in to a transient provider error; the failure path wrote the incident
back to `open` — the one parked state `_PARKED_INCIDENT_STATUSES` omitted,
because `open` is also what a brand-new incident looks like. For eleven hours
every re-firing deduped into it and was discarded: one timeline event, nothing
in Slack. `open`/`investigating` are now *conditionally* parked, decided by
`job_store.has_live_investigation_job`, with a 120s grace window covering the
create→enqueue race. Confirmed by replaying the webhook against the live DB:
the thread that had said "Incident opened" and nothing since received the
notice, the second firing was correctly suppressed by the 60-minute cooldown,
and `incidents_created` stayed 0. The fallback in `record_investigation_job_
failure` (extracted from `_run_graph_impl` to make it testable) also no longer
hard-writes `FAILED` over a row the lease reaper has already requeued.

The five older ones, all committed, none pushed: **#25** retry machinery
disabled by SQLAlchemy identity-map aliasing — `fail_job()`'s `select()` hands
back the caller's own object, so only its *return value* distinguishes;
**#25b** terminal investigation failures now post to Slack instead of leaving
the thread at "Incident opened" forever; **#26** no plan had ever survived
parsing (4/4 runs, `actions` arrived as a JSON string), which is why
`patch_resource_limits` had never run; **#27** the settle floor above, from
`d2fb7c5d`'s "RESOLVED … after 0s"; **#28** a resolved alert did not stop its
own investigation — `bb5d557e` cleared at 17:11:15 and ran 13 more minutes and
five specialists, was retried after a restart, and ended `investigating` with
`resolved_at` stamped, heading for an approval request for an alert that had
stopped firing. Both halves fixed: the resolve paths now cancel, and the
INVESTIGATING write refuses a resolved incident (automatic triggers only — a
person replying in the thread may still reopen one). Confirmed live by
re-running the failure: `0b932c9c` was `investigating` with its job `running`
when the resolved webhook arrived, and 15s later read `resolved` / `cancelled`,
attempt count not consumed, with the cancellation posted to the Slack thread.

**Observability repairs** (live, not in git): promtail uses k8s pod discovery
instead of a hand-maintained allow-list (13/13 pods; the `pod` label the Loki
Specialist queries on now exists). Two silent traps: promtail auto-injects
`spec.nodeName=$HOSTNAME` and in a pod `HOSTNAME` is the *pod* name, matching
zero pods **with no error anywhere** (fix: downward-API env var); and this k3s
runs `--docker`, so the pipeline needs `docker: {}`, not `cri: {}`. Also added
kube-state-metrics + a `cluster-resources` rule group, and a recency guard on
`PodOOMKilled` (`kube_pod_..._last_terminated_reason` fires forever otherwise).

## Active problem
**Narration fidelity is the last open honesty gap, and it recurred twice.** On
`4a0b0254` the supervisor TL;DR claimed "a regression introduced in that
rollout" and "the Loki Specialist found no error logs", discarding the
specialist's actual finding (a 150 MiB warm-up cannot fit a 64Mi limit). On
`555a3acb` seq 15 — the first real in-thread Slack *question* — it answered
"we're still in the investigation phase right now — the incident is marked
`awaiting_approval`, and the execution graph just started" (self-contradictory;
investigation was done and a plan was waiting on a human), then offered a
walkthrough, **never naming the `approve fix` reply that was the one thing
needed**. Same family as #21–#24, and the Slack-only rule makes it
load-bearing. Prompt-only fixes do not hold here (8/8 replays): put the
correction below the model's text, as `narrative._echoed_alert_claims` does.

## Relevant files
`sre_agent/`: `agent_state.py` (LLM-facing schemas + container decoding),
`graph_builder.py` (planner/swarm prompts, fallback plan), `act_phase.py`,
`approval_flow.py` (the Slack message that gates everything), `executor.py`,
`policy_gate.py`, `incident_reconciler.py`, `api/v1/alerts.py` (dedup),
`narrative.py`, `job_store.py`, `war_room_service.py`.

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1233 passed,
  3 skipped** (411s). Health is `/ping` on **port 8080** (`/health` 404s).
- **Task #5 done, live**: `555a3acb` [ocr-extractor] 64Mi→512Mi and `d2fb7c5d`
  [thumb-worker] →256Mi+200m, real `kubectl set resources` after a Slack
  `approve fix`, both pods `1/1 Running` 0 restarts. First live exercise of
  `patch_resource_limits`; #26 unblocked it. `555a3acb` seq 14/15 is also the
  first real in-thread Slack question answered by the Supervisor.
- **Promtail fix improved diagnosis, live**: on `4a0b0254` the Loki Specialist
  reported "6 restarts each dying right after logging 'warming 150 MiB page
  cache', backoff 13s→166s" — logs that never reached Loki before.
- **#25's retry fix is now confirmed live**, incidentally: two API restarts
  killed `bb5d557e`'s investigation mid-flight and job `1a06f204` climbed to
  `attempt_count=3 of 3` and resumed. Before the fix every interrupted job
  died at 1 of 3. (#25b remains unit-tested only —
  `tests/test_investigation_failure_path.py`.)
- `dc1712ca` remains the reference clean run (`patch_deployment_env`,
  `RESOLVED after 330s`, generative runbook).

## Known blockers or risks
- **Severity is always `UNKNOWN` for real Meridian alerts** (rules emit no
  impact/urgency annotations), so the autonomous path never engages. Missing
  annotation in the locally-owned `cluster-resources` rules, or a platform
  defect? Still unanswered.
- **Live mutation surface includes arbitrary env vars** in an allow-listed
  namespace; the edge denylist is the only guard (`EXECUTOR_ALLOWED_ENV_KEYS`
  unset).
- **Alert rules live only in the live `prometheus-config` ConfigMap, not in
  this repo**, and several are stale (`InventoryMemoryApproachingLimit` fires
  above `1e6` bytes while claiming 200MB; checkout's real limit is `768Mi`,
  not the `256Mi` its description asserts).
- **`ANTHROPIC_MODEL=claude-3-5-sonnet-latest` in the API container 404s**, so
  any `create_llm_with_fallback()` default path runs silently degraded. The
  agent path routes via `ExecutionContext`/`route_llm` and is unaffected.
- Known app bug: `checkout-service app.py:147` `int(order_id[-1])` — the real
  cause of `CheckoutHighErrorRate`, correctly root-caused, not agent-fixable.
- `.env.local-backup-20260910` (untracked) holds live secrets and is **not**
  matched by `.gitignore`'s `.env` pattern — never commit it; always use
  explicit paths in `git add`.
- **Nothing has been pushed.** Local-only: `416c84f`, `936eefc`, `0eb6ee5`,
  `50815bd`, `fef1a14`, `3c1cbc7`, `720d361`.

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
Fix the narration-fidelity gap deterministically (above): the supervisor's
Slack answers must not contradict the incident's own recorded status, and any
message sent while an approval is outstanding must name the `approve fix`
reply. Verify by replaying `4a0b0254` and `555a3acb` seq 15 evidence against
the real model — prompt-only fixes have failed this bar before.

Task #4 classes already exercised: slow-query (`dc1712ca`), dependency-down
(`8c925dbd`, `b5287f11`), client-side false-positive (`2c49ac9d` — correctly
proposed **zero** mutations), unhandled exception (`f8ca9a54`), memory-leak
(`d3ca5138`, investigation only), OOM/resource-limit (`555a3acb`,
`d2fb7c5d`). Task #4 is **blocked on a human** replying `mark resolved` on
`d3ca5138`'s Slack thread; the next delivery opens a fresh incident that would
exercise #20 (`escalate` about ConfigMap `meridian-config` rather than a
`config_change` on the `valueFrom`-sourced `CHAOS_MODE`).

Cluster cleanup owed: `thumb-worker` and `ocr-extractor` are healthy at raised
limits; `pdf-thumbnailer` still crashlooping by design. Open incidents:
`9dfa33d4` (investigating), `3b879513`, `4a0b0254`, `47560f97`; `d3ca5138` and
`f8ca9a54` investigated; `555a3acb` and `d2fb7c5d` awaiting a human
`acknowledge`.
