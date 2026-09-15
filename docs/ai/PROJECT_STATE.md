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
Thirty-three defects found by live fire (numbered to #34; #30 was withdrawn on
evidence). Per-defect detail is in git log. The
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
- **Compute the correction from the column, don't ask the model for it**
  (#31, and #23 before it). Being *told* the status does not stop a narrator
  contradicting it — the live seq-15 answer quoted `awaiting_approval` inside
  a sentence claiming two other phases. Anything the database already knows
  is stapled under the model's text, never negotiated with it in a prompt.
- **A call that errored is not a call that was never made** (#32). Three
  consumers each read "no mutation succeeded" and inferred "no mutation was
  attempted" — a status, a report heading, and a learning class. Whenever
  code branches on success, ask what it does with *attempted-and-failed*, and
  whether that is the same sentence as *never tried*.
- **A confirmation is only true at the moment it is sent** (#33). "Approved —
  remediation is running" was composed correctly and posted three minutes
  late, under the finished report, because the call that produced it ran the
  remediation first. For any acknowledgement, ask *when* it reaches the human
  relative to the work it describes — and treat silence on the only channel
  as its own failure, because the human re-sends into it.
- **A signal that is only observed is a signal that is not working** (#34).
  The correlation gate scored the live duplicate at 1.00 and, by design, did
  nothing with it for weeks while the user's actual complaint was the
  duplicates. Shadow mode is the right way to *earn* trust in a signal, but
  it has to end: ask of any shadow-mode component what evidence would promote
  it, then go read whether that evidence has already accumulated.
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

**#31** (fixed, live-confirmed): a follow-up answer contradicted its own
incident and never named the reply that would move it. `555a3acb` seq 15 —
the first in-thread Slack question this platform answered — was *"we're still
in the investigation phase right now — the incident is marked
`awaiting_approval`, and the execution graph just started"*, then a
walkthrough offer, with no mention of `approve fix`. Investigation was done,
nothing was executing, a plan was waiting on a human. `narration_grounding`
maps each `IncidentStatus` to a phase, a plain-English "what is actually
happening", and the Slack reply that moves it, then appends a `---` block
under the model's own text: a ⚠️ line naming each contradicted phase claim,
and a 👉 line naming the exact command. The model's words are never edited.
Wired at `mission_control._traced_chat_reply` — the single seam both
chat-only branches pass through, and the one that produced seq 15 (it routes
around the graph via `_is_chat_only_message`) — and at
`incident_timeline.build_supervisor_direct_answer_content` for the four
in-graph call sites. Verified two ways: the recorded seq-15 text re-grounded
against the live DB now carries both corrections, and **8/8 replays** of
`_build_chat_reply` against the real model with `555a3acb`'s real context
never named the command on their own (8/8 correct after grounding) — the
same 8/8 prompt-only failure rate #23 found. Every command in the map is
asserted against the war room's own regexes, so the reply told to the reader
is one the parser accepts.

**#32** (fixed, replay-confirmed on the real payload): an approved remediation
whose every call failed was described as a fix, an investigation, and a dry
run — by three different surfaces, none of them the one that got it right.
`be398969` ([pdf-thumbnailer] PodCrashLooping): a human approved in Slack at
00:56:51, k3s had died on a Codespace resume, and all three `kubectl` calls
returned `[Errno 111] Connection refused`. `act_phase.summarise_live_execution`
recorded it correctly — *"0/2 mutating action(s) EXECUTED [ERROR]"* — and then
(a) `resolution_report` listed the failed commands under **"What the agent
did:"** with no status marker, so the thread read as though the limit had been
raised to 256Mi while the pod stayed OOMKilled at 64Mi, 68 restarts deep; (b)
`compute_incident_status` returned INVESTIGATED, whose meaning is "nothing was
changed *and nothing was tried*" — and, unlike REMEDIATION_FAILED, it lets
`alert_resolution` mark the incident resolved if the alert ever clears on its
own; (c) `assess_learning_eligibility` graded it `dry_run`, because the
planning pass always leaves read-only actions in `executed`, filing a real
failure as "nothing was ever attempted". All three now branch on
attempted-and-errored: the report leads with **"Nothing was changed on the
cluster"** and marks each line ✅/❌ with the failure reason, the status is
REMEDIATION_FAILED, and the learning class is `failed`. `REFUSED` deliberately
still reads as an investigation — the executor declining to issue a call is
not a call that failed. Confirmed by replaying the recorded act_report through
the deployed container, **and live in the quiet direction**: `67d81c52` was
approved at 01:16:30 and ran 4 read-only/notification actions with nothing
mutating attempted, so the banner correctly stayed off, the heading stayed
"What the agent did", every line rendered ✅, and the status stayed
INVESTIGATED. A correction that cannot stay silent is itself a defect.

**#34** (fixed): the correlation gate watched the duplicates it was built to
stop. Exact-title dedup gives one war room per *title*, but one pod running
out of memory emits two titles — `[pdf-thumbnailer] PodOOMKilled` and
`[pdf-thumbnailer] PodCrashLooping` — so it produced two incidents, two war
rooms, two investigations and two remediations for one wrong memory limit;
live that reached six pdf-thumbnailer threads in 75 minutes. The gate scored
the `88fa9ee4` ↔ `3ed8be00` duplicate at **1.00** and, in Phase A shadow
mode, only wrote a timeline event about it.

Its own shadow record settled which half of the signal to act on — 18
verdicts: **same service, 12 of 12 correct** (`PodCrashLooping` ↔
`PodOOMKilled`; `PaymentServiceHighErrorRate` ↔ `PaymentProviderDown`);
**cross-service text-similarity-only, 3 of 6 wrong** (`[thumb-worker]
PodCrashLooping` ↔ `[ocr-extractor] PodCrashLooping` at 0.62), because a
generic Kubernetes alert renders identical boilerplate whatever it fires on,
so the text score is measuring the alert template, not the incident. So
`incident_correlation.actionable_bundle` returns only the same-service match;
`correlate` and the shadow record are unchanged, and the cross-service signal
keeps shadowing.

A fold needs three things to be true, each of which is a separate guard:
the match is same-service; **something is actually working the parent**
(`_parked_meaning` is None — folding onto a parked incident would mean
nothing works the folded alert either, and unlike exact-title dedup there is
a real choice here); and **the parent has a Slack thread**. The Slack notice
is posted *first* and the fold is conditional on it landing — write-then-
notify inverted on purpose, because a fold the on-call cannot see is worse
than the duplicate thread it saves them. An undelivered notice falls through,
retakes the dedup advisory lock the pre-Slack commit released, and opens the
incident normally. The fold also uses its own window
(`_FOLD_WINDOW_MINUTES`, 120) rather than the scorer's 15: the pool is
already limited to incidents something is actively working, and the live pair
cleared 15 minutes by a hair (01:02 → 01:17) while the two later
pdf-thumbnailer threads fell outside it entirely. The notice says the part that has to be said out loud —
*no separate investigation will run for it* — and offers `mark resolved` as
the way out if the fold was wrong.

**#33** (fixed, from the first successful live remediation): an `approve fix`
reply got no answer for three minutes, and the answer it finally got was
stale. `3ed8be00` ([pdf-thumbnailer] PodOOMKilled) — the human typed
`approve fix` at 01:31:23; Slack said nothing until 01:34:27, then posted
*"✅ Approved — remediation is running"* **underneath** the executor summary
and the full resolution report for a remediation that had already finished
and verified. Ordering, not Slack: `route_fix_approval_command` awaited
`_decide_action_approval_for_incident` → `decide_action_approval`, which
resumes the graph and runs the remediation *and* its 180s verification inline
before returning the message to post. Silence on the only channel invites a
re-send, and did: the same human sent a duplicate `approve fix` on a
neighbouring thread 15 seconds later. `decide_action_approval` now takes an
`on_authorized` callback, awaited by `notify_authorized` immediately after
the CAS commits APPROVED and before the resume — the only claim it makes is
one the CAS has already made true. `notify_authorized` never raises (a Slack
outage must not unwind a committed approval) and returns whether the message
landed, so a receipt that failed to post does not suppress the final one. The
trailing message is skipped only when the receipt actually reached Slack;
every refusal path still posts, and a resume that fails *after* authorization
posts both.

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
**And #32 widened the category**: the worst message this platform has sent —
a resolution report stating a fix had been applied to a service that was
still down — had no model in the loop at all. It was a deterministic template
reading `command` and never `status` from the same dict. Narration fidelity
is not only a model problem; audit assembly code for fields it drops.

## Relevant files
`sre_agent/`: `agent_state.py` (LLM-facing schemas + container decoding),
`graph_builder.py` (planner/swarm prompts, fallback plan), `act_phase.py`,
`approval_flow.py` (the Slack message that gates everything), `executor.py`,
`policy_gate.py`, `incident_reconciler.py`, `api/v1/alerts.py` (dedup, and the #34 fold: `_find_fold_target`,
`_fold_alert_into_incident`), `incident_correlation.py`
(`actionable_bundle` — what the gate is licensed to act on),
`narrative.py`, `job_store.py`, `war_room_service.py`,
`narration_grounding.py` (status → phase/command corrections),
`api/v1/mission_control.py` (`_traced_chat_reply`, the chat-only seam),
`resolution_report.py`, `incident_status.py`, `verified_learning.py` (the
three #32 consumers of a live run's outcome), `war_room.py`
(`route_fix_approval_command` — every inbound Slack command, and the #33
receipt seam).

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1303 passed,
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
- **First end-to-end remediation that both landed and verified, live**:
  `3ed8be00` [pdf-thumbnailer] PodOOMKilled. Human `approve fix` 01:31:23 →
  `2/2 mutating action(s) EXECUTED` → new ReplicaSet at 01:31:24 with
  `limits.memory=256Mi` (was 64Mi, 75 restarts) → pod `1/1 Running`, 0
  restarts → `Verification: RESOLVED — alert PodOOMKilled no longer firing
  after 180s` → `pending_acknowledgment`. Also the **#32 code's first live
  success-direction test**: heading stayed "What the agent did", every line
  ✅, no false-failure banner. The same run surfaced #33 and the duplicate
  below.
- `dc1712ca` remains the reference clean run (`patch_deployment_env`,
  `RESOLVED after 330s`, generative runbook).

## Known blockers or risks
- **The Anthropic API is out of credits** (2026-09-15 01:37). Every new
  investigation dies in seconds with `litellm.BadRequestError … "Your credit
  balance is too low"`; `56d78bd6` [checkout-service] failed three times in
  three seconds. Nothing can be investigated until it is topped up. Slack
  handled it correctly — the thread says "Investigation failed — no findings
  were produced … it needs a human" with the raw error, and invents no
  diagnosis — so this is an account blocker, not a defect.
- **The cross-service correlation signal is still unvalidated** (#34 acted on
  the same-service half only). Text similarity across two services was wrong
  3 times in 6 because a generic alert template dominates the token overlap.
  Shadow recording continues; promoting it needs either a real topology
  adjacency map in the score or a tokenizer that strips the alert template,
  plus a fresh shadow record under that change. Not a blocker for the user's
  complaint — same-service was the whole of the live duplication.
- **A `PodCrashLooping` alert clearing does not mean the pod recovered.**
  Three pdf-thumbnailer incidents auto-closed on
  `alert_cleared_external_verification` while the pod kept OOMKilling:
  CrashLoopBackOff slows the *restart rate* below the rule's threshold, so
  the alert goes quiet on backoff, not recovery.
- **The planner can emit the same mutating action twice and nothing dedupes
  it.** On `3ed8be00` the plan carried two identical `config_change` actions
  and the executor ran both — byte-identical
  `kubectl set resources … --limits=memory=256Mi,cpu=200m`. Harmless because
  it is idempotent; nothing in `policy_gate` or `act_phase` would stop a
  non-idempotent duplicate. Unfixed.
- **A human `mark resolved` writes no timeline event.**
  `mark_incident_resolved_by_human` (`approval_flow.py`) flips status and
  fires side effects; the incident's own timeline records nothing, so there
  is no audit trail of who closed it or that a human did. Every other close
  path writes one (`alert_resolved`). Unfixed.
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
- Known app bug: `checkout-service app.py:147` `int(order_id[-1])` — *a* cause
  of `CheckoutHighErrorRate` (`f8ca9a54`), correctly root-caused, not
  agent-fixable. **Not the only one**: `67d81c52` (2026-09-15) is the same
  alert on the same service from a different fault — every inventory-hold
  call failing and propagating as an unhandled asyncio TaskGroup
  ExceptionGroup through the ASGI handler (traced to commit `4ed89b29`,
  "reserve inventory hold before charging payment"). That run explicitly
  demoted the checksum hypothesis as "not supported by the log signature".
  One alert name can have two open incidents with two different causes.
- `.env.local-backup-20260910` (untracked) holds live secrets and is **not**
  matched by `.gitignore`'s `.env` pattern — never commit it; always use
  explicit paths in `git add`.
- **`master` is pushed and level with `origin/master`** as of 2026-09-15
  (`a989845`), on request. It had carried 33 local-only commits. Keep the
  standing rule: push only when asked, and check
  `git log master --not origin/master` rather than assuming either way.
  `.env.local-backup-20260910` holds live `SECRET_KEY` and
  `CREDENTIAL_ENCRYPTION_KEY`, is untracked, and is **not** matched by
  `.gitignore`'s `.env` pattern — stage explicit paths, never `git add -A`.

## Operating the Codespace
**Run `scripts/codespace_boot.sh` after any resume, and verify k3s before
approving anything.** A resume on 2026-09-15 did not fire `postStartCommand`,
k3s stayed dead with nothing on :6443, and a human's Slack `approve fix` on
`be398969` executed against a dead API server — every call refused (that run
is #32). The check is `sudo k3s kubectl get nodes`. The script's two-attempt
loop is load-bearing: attempt 1 died and attempt 2 stuck, exactly as its
comment predicts, because the node IP had changed (10.0.10.101 → 10.0.1.69).
It also self-heals the Alertmanager webhook when that IP changes. The API
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
**Blocked on Anthropic credits** — no investigation can run until the account
is topped up, so anything needing a live incident waits. #34 is in and
deployed but has *not* yet folded a live alert; the first thing to check once
credits are back is a real `[service] PodCrashLooping` arriving while that
service's `PodOOMKilled` incident is open, and confirming one thread, a
`correlated_alert_folded` timeline event, and no second investigation job.
Code-only work is unaffected: finish Task #4 (below), then audit the **last narration surface** the same way
#31 did the chat one: the aggregate wrap-up
(`build_supervisor_aggregate_content`) still passes model text to Slack with
no status-derived correction. The question to ask: if the model narrates the
wrong phase here, does the reader lose a command they needed? Reuse
`narration_grounding.grounding_footnote`. (The resolution report was the other
half of this task; #32 fixed its *deterministic* half — a report can lie with
no model involved at all, by rendering a field it never read. Whether it also
needs a `grounding_footnote` is still open.)

Also unexplained, seen while replaying: the Langfuse exporter logs `Failed to
export span batch code: 401, reason: Unauthorized` on every narrator call
from inside the API container. Tracing was verified working in Task #8, so
this is either a stale key in the container env or an org-credential
regression — worth one look before trusting any Langfuse trace.

Task #4 classes already exercised: slow-query (`dc1712ca`), dependency-down
(`8c925dbd`, `b5287f11`), client-side false-positive (`2c49ac9d` — correctly
proposed **zero** mutations), unhandled exception (`f8ca9a54`), memory-leak
(`d3ca5138`, investigation only), OOM/resource-limit (`555a3acb`,
`d2fb7c5d`). Task #4 is **blocked on a human** replying `mark resolved` on
`d3ca5138`'s Slack thread; the next delivery opens a fresh incident that would
exercise #20 (`escalate` about ConfigMap `meridian-config` rather than a
`config_change` on the `valueFrom`-sourced `CHAOS_MODE`).

Cluster cleanup owed: `thumb-worker` and `ocr-extractor` are healthy at raised
limits. **`pdf-thumbnailer` is genuinely broken, not by design**: still
OOMKilled at `limits.memory=64Mi`, 0/1 available, ~68 restarts. Its fix was
approved and never landed (#32), and its incident `be398969` sits at
`investigated` — a status the fixed code would no longer produce, left as-is
because approval state is never patched in the DB by hand. Non-resolved
incidents (2026-09-15): `f8ca9a54`, `d3ca5138`, `3d4030a0`, `be398969`
investigated; `3b879513` `open` (its investigation is dead — #29's notice now
fires on it); `555a3acb` and `d2fb7c5d` `pending_acknowledgment`. Owed from
the human in Slack: `acknowledge` on `555a3acb` and `d2fb7c5d`, `mark
resolved` on `f8ca9a54`, `d3ca5138`, `3b879513` and `be398969`.

**Gap this exposed, unfixed:** the act-phase live path has no retry. When its
calls fail there is no Slack command that re-runs the plan — `retry_fix`
exists only in the Temporal `incident_remediation_workflow`, not here — so a
remediation that failed for a transient reason (a dead cluster) can only be
closed with `mark resolved` and re-opened by the next alert firing.
