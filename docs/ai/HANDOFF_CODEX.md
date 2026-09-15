# Handoff prompt — continue the Sentinel/Meridian SRE agent work

Paste everything below the line into Codex as its opening instruction. It is
written to be self-contained; everything it references is in this repo.

---

You are continuing work on **Sentinel**, a multi-tenant LangGraph SRE agent
that investigates Kubernetes incidents for a demo tenant called **Meridian**,
and asks a human to approve any cluster write over Slack. The repo is at
`/Users/jayan/Downloads/Sre-automation`, branch `master`.

## Read these first, in this order

1. `docs/ai/PROJECT_STATE.md` — the durable state file. Its **Active
   problem**, **Known blockers or risks**, **Operating the Codespace** and
   **Next bounded task** sections are current as of 2026-09-15 and are the
   authority on everything below.
2. `docs/ai/DECISIONS.md` — search it for the topic you are touching; do not
   load it whole.
3. Only then open source files, and only the ones on the call path.

Do not start by scanning the repo. The state file already names the files
that matter.

## The method that produced the last five fixes

This codebase has been hardened by finding **honesty defects**: places where
the system reports something it did not actually establish. Thirty-nine are
recorded in `PROJECT_STATE.md` under *Completed or verified work*. The
approach that worked, and that you should keep using:

- **Probe the live shape before you write the fix.** Every defect below was
  confirmed against the running system or a decisive local probe, not
  inferred from reading code. #39 was proven by patching *one field*
  (`candidate.source_digest`) and watching a BLOCK verdict flip to PROMOTE.
- **Be willing to reject the proposed remedy for a narrower one.** The Codex
  audit's P0 #1 asked for a typed `EvidenceRecord` pipeline; the real defect
  (#36) was that the severity gate read the wrong field — the numbers were
  never missing. The narrow fix shipped; the redesign did not.
- **Certifying that evidence exists is not reading it** (#39). Ask of any
  check on a file: *does anything here open the contents?*
- **When a gate has never once blocked honestly, check whether its own
  thresholds are satisfiable** before assuming the evidence was merely lazy.
  An unreachable policy selects for fabricated evidence — that is exactly
  what #39 turned out to be.
- Don't ask a bundle, report, or summary for its verdict. **Recompute the
  verdict from the records**, using validators that already exist.

## Hard constraints — these are not negotiable

- **Slack is the only communication channel this platform has**, for every
  message type. Anything that fails silently in Slack is a defect, not a
  cosmetic issue. "Stopping work silently is the same failure as failing
  silently."
- **Never bypass the approval system in the database.** Do not `UPDATE` an
  incident's status, an approval row, or a job state by hand to move a test
  along. The sanctioned paths are the Slack commands `approve fix`,
  `acknowledge`, `mark resolved`, and the `/approve` and `/mark-resolved`
  endpoints. A DB shortcut invalidates the very thing being tested.
- **A human performs the human actions.** When a run needs someone to reply
  in a Slack thread, post the thread link and the exact command and *wait*.
  Do not simulate the human side.
- **Real GitHub writes need explicit, per-write sign-off** from the user.
  Never ask the user to paste a token or secret into chat.
- **`.env.local-backup-20260910` is untracked and holds live `SECRET_KEY`
  and `CREDENTIAL_ENCRYPTION_KEY`.** `.gitignore`'s `.env` pattern does
  **not** match it. Always stage explicit paths — **never `git add -A`**.
- **Push only when the user asks.** `master` is level with `origin/master` at
  `e263633` as of 2026-09-15, and **CI run `34986611048` on it is fully
  green** — that is your baseline. If CI is red when you arrive, it is
  something you introduced. Check with `git log master --not origin/master`
  rather than assuming either way.
- End every commit message with:
  ```
  Co-Authored-By: <your attribution>
  ```
  Match the existing footer convention in `git log`.

## Task 1 — Defect #40, the largest open defect. Start here.

**A clearing Alertmanager alert cancels the live investigation and throws
away everything it found.**

Measured on the live system over the 12 hours to 2026-09-15 14:45: **15
incidents opened, all 15 auto-resolved** (shortest lifetime 60 seconds), and
the jobs behind them ended **11 `cancelled` to 4 `completed`**. 73% of this
system's investigations are killed mid-flight. This is larger than anything
in the audit, because it does not degrade the output — it discards the work
entirely after paying for it.

### Where it lives

- `sre_agent/approval_flow.py:603` — `fire_resolution_side_effects(incident,
  organization_id, cluster_id)`. Its **first** act is
  `job_store.cancel_incident_investigations`, with the comment *"First,
  because it is the only one that stops work still being done."*
- Three callers:
  - `sre_agent/approval_flow.py:599` — human `acknowledge`
  - `sre_agent/approval_flow.py:753` — human `mark resolved`
  - `sre_agent/api/v1/alerts.py:691` — **the Alertmanager clear** (guarded by
    `decision.mark_resolved` from
    `sre_agent/alert_resolution.py:56 reconcile_resolved_alert`)
- `sre_agent/job_store.py:274` — `cancel_incident_investigations`

Cancelling is **correct for the two human paths** and wrong for the third.

### Why the third path is wrong — three distinct costs

1. **The diagnosis is discarded.** One cancelled run had already root-caused
   that the `InventoryHighErrorRate` rule counts 404s as errors. That finding
   died with the job.
2. **A clearing alert is not recovery.** A pod *restart* clears
   `PodOOMKilled`; CrashLoopBackOff slows the restart rate below the rule's
   threshold, so `PodCrashLooping` goes quiet **on backoff, not on health**.
   This is already recorded in `PROJECT_STATE.md` as
   `alert_cleared_external_verification`.
3. **It silently discards pending human approvals.** Incident `cf58ef6a`
   posted its approval request at 06:17:52 and was resolved at 06:18:56 — the
   window a human had to approve anything was **64 seconds**.

### Fix shape (proposed, not yet built — validate it before you commit to it)

Split the resolve paths:

- **Human resolve** (`approval_flow.py:599` and `:753`) keeps cancelling
  exactly as it does today. A human saying "this is resolved" is an
  instruction to stop.
- **External clear** (`alerts.py:691`) marks the incident resolved and closes
  the war room as it does now, **but lets the in-flight job run to completion
  and post its findings to the thread.** It then **hard-stops at the approval
  boundary**: no approval request is created, and the thread gets a message
  saying the alert cleared, so no cluster write will be proposed.
- If an approval is **already outstanding** when the clear arrives, say so in
  the thread rather than closing silently. A human who was asked a question
  is owed the reason it was withdrawn.

This preserves the invariant #28 exists to protect — **no cluster write for
an alert that has stopped firing** — without burning the investigation.

The natural hard-stop point is `_prepare_approval_node` in
`sre_agent/graph_builder.py:60-160`, which already has a
`not_applicable`/`not_required` early-return branch before
`create_or_reuse_pending_approval`. Re-checking incident status there is
likely cheaper and safer than threading a flag through the graph state — but
confirm that against the live shape first. `sre_agent/act_phase.py:1092`
(`verify_alert_cleared`) is the other place the "alert stopped firing" fact
is already known.

### How to know it worked

- A new incident opens, its alert clears within a minute, and the job ends
  `completed` — not `cancelled` — with its findings in the Slack thread.
- The thread carries an explicit "alert cleared, no write will be proposed"
  message instead of silence.
- **#34's fold fires for the first time.** There are currently **0
  `correlated_alert_folded` events** in the timeline, because nothing stays
  open long enough to be a fold target. If #40 is genuinely fixed, this
  should start appearing.

## Task 2 — Codex audit P0 #4: crash-resumable remediation

`act_phase` has no resume point mid-plan: if the process dies between action
two and action three, nothing knows which actions already ran. The shape of
the fix is to **route `act_phase` through the existing Temporal
`IncidentRemediationWorkflow`**, which already has the durability and a
`retry_fix` signal. Note the related gap recorded in `PROJECT_STATE.md`: the
live act-phase path has **no retry at all** — `retry_fix` exists only in the
Temporal workflow — so a remediation that failed for a transient reason (a
dead cluster) can currently only be closed with `mark resolved`.

Before starting, check the **`sre-temporal-worker` image drift** noted in
blockers.

## Task 3 — the four P1s from the audit, in descending feasibility

1. **Dead operational-reflection branch** —
   `sre_agent/graph_builder.py:1071-1212` and `1806-1849`. The reflector's
   "deeper investigation" loop-back is collapsed to a hard
   `workflow.add_edge("reflector", "planner")` at ~line 1841. The audit calls
   this a dead branch; the code comment says it is a deliberate v1
   simplification. **Both are true.** Decide whether to wire the
   `investigation_swarm` loop or delete the unreachable code — leaving
   unreachable code that looks live is itself an honesty defect.
2. Artifact-backed context (instead of stuffing context into state).
3. Observability semantics.
4. No single owner of job completion.

## Task 4 — the last narration surface

`#31` fixed the chat narration; `#32` fixed the *deterministic* half of the
resolution report (a report can lie with no model involved at all, by
rendering a field it never read). Still unaudited: the aggregate wrap-up,
`build_supervisor_aggregate_content`, which passes model text to Slack with
no status-derived correction. The question to ask: **if the model narrates
the wrong phase here, does the reader lose a command they needed?** Reuse
`sre_agent/narration_grounding.grounding_footnote`.

Prior recurrences, for calibration: on `4a0b0254` the supervisor TL;DR
claimed "a regression introduced in that rollout" and "the Loki Specialist
found no error logs", discarding the specialist's actual finding (a 150 MiB
warm-up cannot fit a 64Mi limit).

## Task 5 — live-fire verification still owed

#35, #36, #37 and #38 are deployed. #38's audit half is **live-confirmed**:
`agent_audit_logs` went SUCCESS 2261 → 2507 while PENDING stayed 18 and
FAILURE stayed 8 — 246 more live tool calls, **zero new orphan rows**.

Never observed live, and still owed:

| Path | What to look for |
|---|---|
| #38 refusal → `CANCELLED` | **No `CANCELLED` row exists yet** in `agent_audit_logs` |
| #35 dead tool → `FAILURE` | No FAILURE since 05:11, which was *pre-fix* |
| #37 write guard → `REFUSED` | `REFUSED`, not `FAILURE`, for a policy refusal |
| #36 severity evidence | An evidence link whose `source` starts `tool:` |

The **18 historical orphan PENDING rows stay in place as evidence.**
Rewriting audit history to tidy a dashboard is the wrong trade.

## Smaller known-unfixed items

- The `sre-temporal-worker` image has drifted from the repo.
- An MCP server that returns an **error string** still launders a failure
  into an apparent success at the boundary #35 did not cover.
- The planner sometimes emits **duplicate mutating actions** in one plan.
- A human `mark resolved` writes **no timeline event**.
- The Langfuse exporter logs `Failed to export span batch code: 401,
  reason: Unauthorized` on every narrator call **from inside the API
  container**, though tracing was verified working in Task #8. Either a stale
  key in the container env or an org-credential regression — worth one look
  before trusting any Langfuse trace.

## Offered and declined so far

`PROJECT_STATE.md` is ~7,100 words against an 800-word target. Splitting it
into a pointer index plus `docs/ai/DEFECTS.md` was offered and not taken up.
Ask before doing it.

## Environment — read `PROJECT_STATE.md` § *Operating the Codespace* in full

The condensed version:

- **k3s was down as of 2026-09-15 14:50** — nothing on `:6443`. Run
  `scripts/codespace_boot.sh`, then verify with `sudo k3s kubectl get nodes`.
  **Do this before any live approval**: an approval executed against a dead
  API server is exactly how defect #32 happened.
- `systemctl` **does not work** in the Codespace container — use `service`.
- The API container `sre-agent-api` is **image-baked with no source mount**.
  Each change needs base64 → `docker cp` into `/app` → md5 → `py_compile` →
  `docker restart sre-agent-api`. Deploy with per-chunk `>` writes, **never
  `>>`** — the ssh wrapper retries, and a retried append once duplicated a
  payload 4×.
- No `curl` inside the API container. Interpreter is `/app/.venv/bin/python`.
- DB: `docker exec -i sre-postgres psql`, user `sre_user`, db `sre_platform`.
  `agent_audit_logs`' time column is **`timestamp`**, not `created_at`;
  `incidents` has no `resolution_type`; `clusters` has no `organization_id`;
  `approval_requests` has no `action_type`;
  `incident_timeline_events.payload_json` is **text**, so cast
  `(payload_json::jsonb)`. **Confirm a column in `information_schema.columns`
  before querying it** rather than guessing from the ORM.
- Slack workspace `meridian-iif3801`, channel `C0C0KA474LC`. The only
  namespace is `meridian`.
- Locally, the interpreter is `.venv/bin/python`; bare `python` is not on
  PATH.

## Verification — run all of these before claiming anything is done

```bash
.venv/bin/python -m pytest tests -q -p no:cacheprovider       # 1426 passed, 3 skipped (~426s)
.venv/bin/python -m pytest tests/integration -m integration -q # 13 passed
.venv/bin/python -m pytest tests/test_docs_truthfulness.py -q  # 6 passed
bash scripts/check_python_quality.sh                           # ruff + mypy + compileall
bash scripts/check_eval_smoke.sh                               # 44 invariants
bash scripts/check_no_static_secrets.sh                        # greps tracked files for credential shapes
```

The secret scan runs **early** in CI's `backend-tests` job, so when it fails
nothing after it executes and the coverage/integration steps silently do not
run. If you need a credential-shaped string as test data, assemble it at
import time from parts rather than writing a literal or adding an exclusion
— see `tests/test_tracing.py` and commit `e263633` for the pattern.

Those numbers are the current baseline. A new fix should move the first one
up by exactly the number of tests you added, and move nothing else.

## When you finish a piece of work

Update `docs/ai/PROJECT_STATE.md` — **replace stale sections, do not append a
diary.** Record the defect, the evidence that proved it, the fix, and the
verification numbers. Then state plainly what is done, what is verified, and
what is still owed from a human.
