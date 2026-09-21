# Handoff prompt — continue the Sentinel/Meridian SRE agent work

Paste everything below the line into Codex as its opening instruction. It is
written to be self-contained; everything it references is in this repo.

Last updated 2026-09-19, after the four-phase pass, the Codex reconcile
merge, and #53/#54. `PROJECT_STATE.md` is the concise authority.

**All four phases are done and deployed.** The phase sections below are kept
as the record of what was built and why; they are not your task list. Your
task list is **"The current plan"**, further down — read that first and read
it whole, because its Step 0 blocks everything after it.

---

You are continuing work on **Sentinel**, a multi-tenant LangGraph SRE agent
that investigates Kubernetes incidents for a demo tenant called **Meridian**,
and asks a human to approve any cluster write over Slack. The repo is at
`/Users/jayan/Downloads/Sre-automation`, branch **`master`** (`2ba66f4`) —
PR #55 merged the reconciliation on 2026-09-20 with all 18 CI checks green,
so Codex's 24 commits, the Codespace's 20, #53/#54 and the routing fix are
all on `master`. Every other branch was fully contained in it and has been
deleted; `master` is the only branch on the remote. Whether it becomes a PR into `master`
is the user's decision, not yours.

## Read these first, in this order

1. `docs/ai/PROJECT_STATE.md` — the durable state file, rewritten 2026-09-19
   and current. Its **Active problem**, **Known blockers or risks** and
   **Next bounded task** sections are the authority on everything below.
2. `docs/ai/DECISIONS.md` — search it for the topic you are touching; do not
   load it whole. The newest entry ("Meridian runbooks prescribe one branch
   per fault") covers Phase B including its known limit.
3. Only then open source files, and only the ones on the call path.

Do not start by scanning the repo. The state file already names the files
that matter.

## The standing directive — four phases, in this order

The user's words, verbatim:

> "for our current meridian repo and the issues we send, check if the
> runbooks strictly address the solution. if not, make them a proper solution
> so model doesnt need to think on its own. also, check how holmesgpt has
> implemented context engineering. we need to implement that level of context
> engineering. fix the raised issues first and then proceed with these tasks.
> after all these are done, tell me how it improves. once finalized, we will
> do the run"

- **Phase A — fix the raised issues. DONE, not committed.** #45 the runbook
  was truncated to 500 raw bytes before reaching the agent; #46 specialists
  never saw prior specialists' findings; #47 unbounded tool results entered
  the ReAct transcript.
- **Phase B — make the runbooks prescribe the solution. DONE, not committed,
  NOT PUBLISHED to Notion.** See *Where Phase B actually stands*.
- **Phase C — match HolmesGPT's context engineering. DONE locally, not
  committed.** See the newest context-engineering entry in `DECISIONS.md`.
- **Phase D — report how it improves. DONE at the design/verification level.**
  The largest observed result is bounded ~98.9% smaller in the model view and
  the working history budget is 67.7% below the old ceiling. A live cost claim
  still requires a deployed smoke run; the user decides when to do it.

**Hard constraint: the user refused a $632 campaign — "i am not going to
spend 632 dollars".** They still want the benchmarking ("but i do need to do
benchmarking"). The job is to make it affordable, not to drop it. Cost
reduction is a requirement of Phase C, not a nice-to-have.

## Phase C — implemented result

**The measured target.** The ReAct loop re-sends the whole transcript every
iteration. On the validation run, **8 of 114 calls (7%) carried 1,295,598
tokens — 37% of all input and $3.68 of the $7.90 (47%)**. Any real cost
reduction has to attack that tail; trimming the prompt prefix cannot.

For scale, and for the shape of the problem: the curated runbook reached the
investigating agent as **500 bytes** while a raw log dump reached it as
**1,782,133 bytes** — a **1:3,564 ratio in favour of noise**. Phase A closed
the worst of that. Phase C closed the remaining bounded task with:

- source-side discipline in every specialist brief: narrow labels/time range,
  aggregate/pattern tools before raw listings, and small explicit limits;
- the real Loki contracts in the logs prompt (`query_logs` with
  `start_time`/`end_time`/`limit`, and aggregate-first
  `analyze_log_patterns`) instead of nonexistent `search_logs` arguments;
- truthful elision markers: raw bytes remain in the audit artifact but are not
  visible to the current model, which must re-query narrowly rather than infer
  absence;
- invocation-scoped capture of every `FitReport`, including unchanged calls,
  aggregated per specialist in `metadata.context_fitting` and persisted in
  the job result on success and failure.

A follow-up cost defect is also closed locally: specialist ReAct calls are
marked with a task-local investigation scope, and the shared MCP wrapper now
rejects open-ended logs/metrics, unbounded commit history, untargeted pod/event
listings, and vague runbook searches. Prompts name the real tool signatures;
the alert brief supplies exact labels, timestamp, selected runbook, and prior
findings. Broad Kubernetes inventory and stale runbook convenience tools are
not exposed. Deterministic post-remediation verification bypasses this model
search gate while keeping namespace isolation, so process-death idempotency
and Task #40's post-clear no-remediation invariant are unchanged. Scope
refusals are audited as `REFUSED` and reach the model without cancelling a
sibling tool call. See the matching decision in `DECISIONS.md`.

HolmesGPT was inspected at upstream commit
`3bd44edf04f9587c778ee8e9b244965190c40fdf`. Sentinel deliberately rejected
worker-local spill-file pointers (remote MCP/multi-worker processes cannot
read them reliably) and mid-loop LLM summarization (extra spend and weaker
prompt-cache reuse). Its durable evidence artifact plus deterministic fitting
are the correct equivalents here.

**Measure before and after against the recorded baseline.** Phase 0 run
`5cc643c5`: 111 calls, **$4.77**, 20.8 min, 3.84M tokens, split 2.0%
uncached / 27.0% cache creation / 71.0% cache read → effective input
multiplier **0.429**. Cost lands in `jobs.result` JSON via `model_accounting`
— **Langfuse is not the system of record for tokens**; an earlier claim that
it was has been retracted.

## Where Phase B actually stands — read this before touching runbooks

Measured with `scripts/audit_runbook_coverage.py`:

| | live Notion corpus | rewrites in `runbooks/meridian/` |
|---|---|---|
| Retrieval correct | 22/22 | 22/22 |
| Prescribes the solution | **0/22** | **22/22** |
| No probe in a verification section | 22/22 | 0 |
| Missing ≥1 prohibition | 20/22 | 0 |
| No coherent branch | 10/22 | 0 |

**The rewrites are local files. The agent still retrieves the 0/22 pages from
Notion at run time, so none of Phase B is in effect.** Titles in
`runbooks/meridian/*.md` match the four live Notion pages exactly, so
publishing replaces bodies without disturbing the verified 22/22 retrieval.
**Writing to Notion is outward-facing and needs the user's explicit
go-ahead, which has not been given** — task #44. Do not publish on your own
initiative. Also undecided: what to do with the 16 `RB-AUTO-*` pages now that
ranking no longer promotes them.

**Do not trust a pass count from the grader alone.** An earlier version of
`audit_runbook_coverage.py` reported 22/22 while blind to a deleted
prohibition; the honest score then was 12/22. `scripts/audit_runbook_controls.py`
deletes one property at a time and asserts the named scenario flips
PASS→FAIL. **Run it after any change to the grader or to the runbooks.** One
control is documented `KNOWN_BLIND`: the grader reads content, not routing —
it cannot verify that the decision procedure would send a given fault to the
branch that satisfies its contract. Only a live run exercises routing.

Three agent-side defects were found while writing the runbooks, any one of
which would have made a prescriptive runbook unusable:

- `clamp_min` was missing from `_ALLOWED_FUNCS` in `sre_agent/nl_query.py`;
- `le` inside `sum by (le)` parsed as an unknown metric. Together these made
  **16 of 22 recovery probes unrunnable** — a runbook could name the right
  query and `validate_promql` would reject it. Both fixed.
- `DEFAULT_RUNBOOK_BRIEF_MAX_CHARS` 6000 → 9000. Every `Branch X — Action:`
  heading scores priority 0 in `_SECTION_PRIORITY`, so five branches spent
  the budget and **Verification was dropped** — the agent got every
  remediation option and lost the probe that decides whether the one it chose
  worked. `tests/test_runbook_context.py` now fails if a shipped runbook
  outgrows the budget (verified: it fails at 6000).

A runbook can only prescribe a query that passes `validate_promql`. Check
`_ALLOWED_METRICS` and `_ALLOWED_FUNCS` before writing one into a page.

## The method that produced the last several fixes

This codebase has been hardened by finding **honesty defects**: places where
the system reports something it did not actually establish. The approach that
worked, and that you should keep using:

- **Probe the live shape before you write the fix.** Every defect was
  confirmed against the running system or a decisive local probe, not
  inferred from reading code. #39 was proven by patching *one field*
  (`candidate.source_digest`) and watching a BLOCK verdict flip to PROMOTE.
- **Build the negative control before you believe the number.** Phase B's
  22/22 is only meaningful because deleting a prohibition makes it fail.
  A measurement that cannot fail is not a measurement.
- **Be willing to reject the proposed remedy for a narrower one.** The Codex
  audit's P0 #1 asked for a typed `EvidenceRecord` pipeline; the real defect
  (#36) was that the severity gate read the wrong field — the numbers were
  never missing. The narrow fix shipped; the redesign did not.
- **Certifying that evidence exists is not reading it** (#39). Ask of any
  check on a file: *does anything here open the contents?*
- **When a gate has never once blocked honestly, check whether its own
  thresholds are satisfiable** before assuming the evidence was merely lazy.
  An unreachable policy selects for fabricated evidence.
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
  Do not simulate the human side. Approval
  `fcbc45a7-1c13-4cdc-9548-026f28fb4352` on incident `281b8110` is the
  user's, in Slack — neither to simulate nor to bypass.
- **Writing to Notion is outward-facing and needs explicit per-write
  confirmation.** Every corpus read so far has been strictly read-only.
- **Real GitHub writes need explicit, per-write sign-off** from the user.
  Never ask the user to paste a token or secret into chat.
- **`.env.local-backup-20260910` is untracked and holds live `SECRET_KEY`
  and `CREDENTIAL_ENCRYPTION_KEY`.** `.gitignore`'s `.env` pattern does
  **not** match it. Always stage explicit paths — **never `git add -A`**.
- **Commit and push only when the user asks.** Nothing from Phase A, B or C is
  committed; the intent is to deploy them together once.
- End every commit message with the attribution footer your harness gives
  you, matching the existing convention in `git log`.

## Decisions that belong to the user — raise them, do not action them

Both of the decisions this section used to carry are now **settled**: the four
runbooks are published to Notion (a post-write dump scores 22/22), and
`ANTHROPIC_PROMPT_CACHE_TTL` is back to `5m` on the Codespace — the `1h`
override measured **$7.90 against a $4.77 baseline**, because the longer TTL
bought more cache *creation*, which bills at a premium, not more reads.

Three remain open, and none of them is yours to action:

1. **Rebuild and redeploy the Codespace stack.** It restarts everything the
   user is running. Asked on 2026-09-19 and not yet answered. See Step 0.
2. **The campaign shape and budget** — roughly $88 for calibration support
   versus the $194/$389/$583 paired-ablation tiers. See Step 4.
3. ~~Whether the reconciliation branch becomes a PR into `master`.~~
   Settled 2026-09-20: merged as PR #55, branches cleaned up.

## Deferred backlog — real, unfixed, and not part of Phase C

Do not pick these up without asking; they predate the four-phase directive.
Entries marked CLOSED were re-checked against the code on 2026-09-20 and the
claim did not survive; they are kept rather than deleted because this section
was being read as current and is where the wrong claims came from.

- **Dead operational-reflection branch** — CLOSED 2026-09-20; the branch is
  live. `_route_reflector` (`sre_agent/graph_builder.py:1137`) is registered
  with `add_conditional_edges` at `:2250-2257`, and `:2258` closes the loop
  with `investigation_swarm -> reflector`. Both are gated on
  `ablation.reflector`, which is what an ablation arm needs. There is no hard
  `add_edge("reflector", "planner")` to delete.
- **`build_supervisor_aggregate_content`** — CLOSED 2026-09-20; no such
  function exists, so "the last unaudited narration surface" pointed at
  nothing. The two real surfaces are
  `incident_timeline.build_supervisor_summary_content` and
  `build_supervisor_direct_answer_content`, and `grounding_footnote` is
  applied to both (`sre_agent/incident_timeline.py:552` and `:597`).
- **Task #32** — CLOSED 2026-09-20; implemented end to end, and the chain is
  now tested. `approval_flow.py:681` calls
  `job_store.cancel_incident_investigations`, which stamps
  `cancel_requested_at` and deliberately leaves a RUNNING job alone — the
  worker owns the lease, so the worker has to retire it. It observes the flag
  through the lease heartbeat (`job_worker._renew_job_lease` ->
  `heartbeat_job` raises `DurableJobError`), cancels the investigation task,
  and `fail_job` writes CANCELLED without consuming a retry. The debounce this
  entry asked for already exists as the exemption at
  `sre_agent/api/v1/alerts.py:696-698`: an *external* alert clear retires
  remediation authority but keeps the evidence-gathering job alive, so a
  flapping alert cannot abort a legitimate diagnosis. Only a human
  `mark resolved` cancels. Covered by
  `tests/test_job_worker.py::test_a_cancel_request_stops_the_investigation_mid_flight`
  and `tests/test_resolved_incident_stops_investigation.py::test_a_cancelled_run_is_retired_rather_than_retried`.
- **SLO subsystem** — NARROWED 2026-09-20; "dead" is wrong and three of the
  five specifics no longer hold. `GET /slos/{id}/status` re-queries the
  cluster's own Prometheus on every poll and persists the result
  (`sre_agent/api/v1/slos.py:87-107`), so `last_calculated` is not frozen, and
  the percent conversions at `:98-104` are internally consistent. A live burn
  rate does exist on the severity path: `severity_telemetry.py:174` produces
  `slo_burn_rate` and `severity_engine.py:220` scores on it. What is still
  unfixed is narrower — `burn_rate_1h`/`burn_rate_6h` are hardcoded `None` at
  `slos.py:112-113` even though the severity path already computes a burn
  rate, there is no latency SLI, and no SLO tool is exposed to the agent.
- ~~Artifact-backed context instead of stuffing context into state;
  observability semantics; no single owner of job completion.~~ **CLOSED
  2026-09-20, all three clauses.** They had drifted apart and were checked
  separately.
  - *Artifact-backed context* — done by Phase C. Large tool results no longer
    ride in state; they are capped on the way into the ReAct transcript
    (task #47) and the specialist prefix is cached rather than re-sent
    (`agent_nodes.py:241-244`). Measured against the pre-Phase-C baseline on
    the same workload: uncached input tokens −67%, cache creation −86%, cost
    per model call −62%.
  - *Observability semantics* — done by `b96a40e` ("close eight Langfuse
    instrumentation gaps found by trace audit") and `eb7c0c1` ("name Langfuse
    specialist observations"). `sre_agent/tracing.py` is 757 lines covering
    trace identity, the root observation, export-stage masking and span
    filtering, with 40 tests in `tests/test_tracing.py`, no TODO markers, and
    per-org Langfuse project isolation that does **not** fall back to an
    operator default.
  - *Single owner of job completion* — done. `update_job_status` and
    `JobStatusUpdate` are deleted outright, so the second writer cannot come
    back; `record_investigation_job_success` in `agent_runtime.py` is now the
    only path that writes a terminal state, and it raises `DurableJobError`
    after commit rather than letting a losing writer overwrite. Six tests in
    `tests/test_job_completion_ownership.py`.
- An MCP server that returns an **error string** still launders a failure
  into an apparent success at the boundary #35 did not cover.
- The planner sometimes emits **duplicate mutating actions** in one plan.
- A human `mark resolved` writes **no timeline event**.
- Unproven under live fire: **#41 only** (planner proposes only non-mutating
  actions — confirmed as behaviour, unconfirmed as a cause).
  **#44 no longer needs live fire.** Waiting for a real 529 meant waiting for
  an outage we do not control, so `tests/test_llm_retry.py` drives the real
  chain — our env, our builder, the SDK's retry — against a fake transport
  that returns `[529, 529, 200]` and asserts the investigation survives with
  all three requests actually sent. A paired zero-budget test
  (`test_a_zero_budget_really_does_fail_on_the_first_529`) proves the harness
  can fail, so a green result is not the test declining to look; 429, 500 and
  503 are covered by the same parametrize.
- ~~Missing-data is measured by no corpus; v2 is frozen and SHA-pinned, so
  closing that needs a v3.~~ **v3 authored 2026-09-20; not yet runnable.**
  `benchmarks/datasets/v3/` is v2's 22 scenarios plus three
  `taxonomy.category = missing_data`, one per split, and it validates under
  the strict loader (`--version v3`, digests repin to byte-identical). v2 is
  untouched and stays the `BENCH_DATASET_VERSION` default.
  Two things worth knowing before touching it:
  - **The enabling change is in a different repo and is not shipped.** No
    existing knob could produce absent telemetry — every one of them degrades
    behaviour and leaves the exporter up. The new `metrics_enabled` knob makes
    checkout's `/metrics` answer 503 while the service keeps serving, so the
    target's `up` drops to 0 and its series go *absent, not zero*. It lives in
    `jayanth922/meridian-shop` (`services/checkout-service/app.py`),
    uncommitted and unpushed, and the checkout image has not been rebuilt.
    **Until that ships, injecting the knob is a no-op and a v3 run would
    silently measure nothing.**
  - **A missing-data probe must never read the service it silenced.** The
    recovery oracle fails closed on an empty result, so such a probe voids the
    trial with `INVALID_SCENARIO` before the agent is graded at all. Every
    scenario in the category therefore puts the fault on one service and the
    dead exporter on another. Asserted by
    `tests/test_scenario_mix_coverage.py::test_a_missing_data_probe_never_reads_the_service_it_silenced`.

## Environment — the traps that have cost time

- **Two full platform stacks exist; only the Codespace one is live.**
  Alertmanager's webhook and `BENCH_BASE_URL` point at it. Deploys, API
  writes and log greps all belong there; the Mac stack is a dev copy whose
  database receives none of it. Assuming one stack has produced a wrong
  conclusion before.
- **A local commit is not a deployment.** The Codespace keeps its own commit
  history; files reach it by copy. Task #37 was closed on a local-only change
  — `MEASURED_INCIDENT_FLOOR_SEC` has 4 refs locally, **0 on the Codespace**.
- **`docker exec … python` is not the agent.** Use
  `docker exec sre-agent-api /app/.venv/bin/python`. The container's bare
  `python` lacks the project's dependencies and reports a degraded skill
  store indistinguishable from a real regression — it produced a false
  "semantic recall is dead in production" finding, since retracted.
- **Never run `docker compose` in `platform/` without `--env-file ../.env`**
  (or `set -a && . ../.env && set +a` first). A service's `env_file:` does
  **not** feed `${VAR}` interpolation — that reads only compose's own
  environment. `DATABASE_URL` silently loses its database name, and
  `${POSTGRES_USER}`/`${POSTGRES_PASSWORD}` in an `environment:` block
  resolve empty, after which asyncpg falls back to OS user `root` and
  `sre-agent-api` crash-loops on `InvalidPasswordError`. The Temporal worker
  stays healthy on the same image, which makes it look like an API bug.
- **`sre-postgres` publishes no host port.** Scripts run from the Codespace
  *host* shell need `POSTGRES_HOST=<container IP, 172.18.0.7>`;
  `_build_database_url` otherwise defaults to the compose hostname
  `postgres`, which does not resolve outside the network.
- **The API's health path is `/ping` on 8080, not `/health`** (`/health` 404s).
  `mcp-loki` declares no healthcheck at all, so it reports `Up`, never
  `healthy` — that is not a failure.
- **macOS AppleDouble files (`._*.py`) ride along in copies** and break
  `compileall` with "source code string cannot contain null bytes". They are
  never importable, but find and delete them before a build.
- Deploy with `bash scripts/deploy_agent_runtimes.sh`; it exports
  `SENTINEL_CODE_SHA` from git HEAD. A bare `docker compose build` bakes
  `code_sha=unknown`, and `ablation_eval.py:320` rejects an arm-vs-control
  comparison at differing `code_sha`. **Freeze code before the campaign.**
  Env-var changes do not move `code_sha`. Verify with
  `python3 scripts/check_runtime_parity.py` on the Codespace host.
- **Every Codespace resume can kill k3s.** Run `scripts/codespace_boot.sh`,
  then verify with `sudo k3s kubectl get nodes`. Do this before any live
  approval — an approval executed against a dead API server is exactly how
  defect #32 happened. `systemctl` does not work in the container; use
  `service`.
- `gh codespace ssh`: **no single quotes survive the wrapper.** Base64-encode
  locally, then `echo <B64> | base64 -d > /tmp/x.py`. Never
  `pkill -f sre_bench.py` — it self-matches. Multi-file `gh codespace cp` can
  silently no-op; copy one file at a time and verify remote content. Deploy
  with per-chunk `>` writes, **never `>>`** — the ssh wrapper retries, and a
  retried append once duplicated a payload 4×.
- The API container `sre-agent-api` is **image-baked with no source mount**.
  Each change needs base64 → `docker cp` into `/app` → md5 → `py_compile` →
  `docker restart sre-agent-api`. No `curl` inside it.
- DB: `docker exec -i sre-postgres psql`, user `sre_user`, db `sre_platform`.
  Table names do not match the ORM class names you would guess: it is
  **`incident_timeline_events`** (not `timeline_events`) and
  **`approval_requests`** (not `approvals`). A wrong name returns a relation
  error that is easy to misread as an empty table.
  `agent_audit_logs`' time column is **`timestamp`**, not `created_at`;
  `incidents` has no `resolution_type`; `clusters` has no `organization_id`;
  `approval_requests` has no `action_type`;
  `incident_timeline_events.payload_json` is **text**, so cast
  `(payload_json::jsonb)`. **Confirm a column in `information_schema.columns`
  before querying it** rather than guessing from the ORM.
- Slack workspace `meridian-iif3801`, channel `C0C0KA474LC`. The only
  namespace is `meridian` — **executor tools default to `demo-app`**, and a
  call that omits the namespace silently targets nothing.
- Locally the interpreter is `.venv/bin/python`; bare `python` is not on
  PATH.
- **There is no `pytest` anywhere on the Codespace** — not the host
  `python3`, not `.venv`, not inside any container. The suite runs on the Mac
  only. A Codespace-side session cannot verify a code change by testing it,
  which is why #53 and #54 were proved with purpose-written before/after
  harnesses against recorded trial bytes instead.
- **The dashboard's data layer is axios, not `fetch`.** It is one shared
  client, `export const api = axios.create({ baseURL: "/api/v1" })` in
  `dashboard/lib/auth-context.tsx:54`, and `dashboard/next.config.ts` rewrites
  `/api`, `/auth`, `/metrics` and `/agent` to `API_URL`. Grepping for
  `fetch(` finds 3 hits and makes the UI look like an empty shell; the real
  figure is **49 axios calls across ~25 endpoints**. Grep for `api.get(` /
  `api.post(`, and scope the grep to `dashboard/app` and
  `dashboard/components` — bare `dashboard/` drags in `node_modules`.
  Likewise, `placeholder` in that tree is overwhelmingly the JSX
  `placeholder=` prop, not a stub.

## The current plan — backend, then frontend, then benchmark

This section supersedes the ordering implied by everything below it. The user
set it on 2026-09-19, verbatim:

> my point is if the backend functionality is working as intended (every
> component), we focus on wiring the frontend properly and then do
> benchmarking

Benchmarking is therefore **last**, even though it is the decision with money
attached and the most tempting thing to plan for.

### Step 0 — rebuild and redeploy — DONE 2026-09-20

**Resolved.** `bash scripts/deploy_agent_runtimes.sh` ran clean in 94s and
`check_runtime_parity.py` passed at
`code_sha=9e7e3d55d07f9d364d8639ca25190461625e42c7`
`fingerprint=9cf36dc029370f64e31a2b5c4e8282229acd6b1f89a7144e7fc181fc1dd731fc`
`files=157` — a clean full sha equal to HEAD, with no `-dirty` suffix, on both
the API and the Temporal worker. The stack is now running identifiable code
and component checks against it mean something. Keep the rest of this step as
the standing procedure and the reason it matters.

Two things the rebuild surfaced, both now handled:

- **k3s was down** — every `meridian` pod `Exited (255)`, the API server
  refusing on 6443. `sudo bash scripts/codespace_boot.sh` recovered it; all 15
  pods are `Running 1/1`. Always check this after a resume, and check it
  *before* concluding anything about the agent's tools.
- **The Codespace node IP had changed**, `10.0.1.129 → 10.0.1.58`, so the
  Alertmanager webhook was pointing at an address that no longer exists.
  `codespace_boot.sh` repoints it automatically — which is exactly why it must
  be run rather than hand-starting k3s. Alert delivery was silently broken
  until it ran.
- `/app/reports` is not a volume, so it was copied to
  `/home/vscode/reports-backup-20260920-060115/` (`run-trace.jsonl`,
  `model-accounting.jsonl`, 464K) before the recreate. Do this every time.

The original finding, kept because it is the reason this step exists — the
Temporal worker's preflight used to report:

```
code_sha=bc18e30-dirty-p0p1fix
fingerprint=ad5097880c74dbef95672455198119f65d9b14c26df3a518cfc723cd21a07b60
files=157
```

`bc18e30` was 11+ commits behind the reconciliation branch, and the entire Codex
merge lands after it. The `-dirty-p0p1fix` suffix is the worse half: the image
was built from an uncommitted working tree, so **what is actually deployed
cannot be recovered from the sha**. Every component you verify against this
stack is a component verified against code nobody can name — which is exactly
the failure mode the user's "is every component working as intended" question
is trying to close.

Rebuild with `bash scripts/deploy_agent_runtimes.sh` (it exports
`SENTINEL_CODE_SHA` from git HEAD; a bare `docker compose build` bakes
`code_sha=unknown`, and `ablation_eval.py:320` rejects an arm-vs-control
comparison at differing `code_sha`). Then confirm with
`python3 scripts/check_runtime_parity.py` on the Codespace host, and re-run
the sweep in Step 2 — every number in it predates the rebuild.

This restarts the user's stack, so it needs their go-ahead each time. It was
given on 2026-09-20 for the rebuild above; it does not carry forward.

### Step 1 — run the test suite — DONE 2026-09-20, by CI

**Resolved, and not the way this step expected.** The suite had never run
against the reconciled tree on any machine, and the Codespace has no pytest —
but PR #55 ran it in CI: **1,966 passed** plus 19 integration tests, 41
warnings, 65% coverage, all 18 checks green (run `35494662328`).

That first run also found why nobody had seen a green suite: the backend job
sets `LLM_PROVIDER=anthropic` and an `ANTHROPIC_API_KEY`, but synced only the
`dev` and `temporal` extras. `langchain-anthropic` lives in the optional
`anthropic` extra, so seven tests — the four ablation graph-shape tests, the
reflector-loop wiring test and both `llm_retry` backend tests — had been
failing on **every** run, `master` included, with `ModuleNotFoundError`. CI
had never exercised the backend production runs on. Fixed by syncing
`--extra anthropic`; `uv.lock` already pinned it, so `--frozen` still
resolves.

**Use CI as the test gate from here.** Push the branch and read the checks;
a local run is no longer the only option. The original instructions are kept
below for the case where you need to run it on the Mac anyway:

```bash
.venv/bin/python -m pytest -q
```

The baseline is 1,975 passed / 37 known warnings. #54 added 7 tests, so the
expected result is **1,982 passed and nothing else moving**. One caveat: the
new `test_grader_read_only_set_matches_the_executor_and_the_policy_gate`
imports `sre_agent.executor` and `sre_agent.policy_gate` from inside
`tests/test_structured_grading.py` for the first time. It does so function-
locally, mirroring `tests/test_policy_gate.py:233-236`. If that import turns
out to be too heavy for the benchmark test module, fix the test — do not
delete the assertion, because the read-only set is mirrored in three modules
and nothing else stops them drifting.

### Step 2 — verify the backend component by component

A read-only sweep, first run 2026-09-19 and **re-run after the rebuild on
2026-09-20 with every number unchanged**, finds the platform up and holding
real data:

| check | result |
| --- | --- |
| compose services | 14 of 15 healthy; `mcp-loki` declares no healthcheck, which is not a failure |
| API | `/ping` → `{"status":"healthy"}` |
| migrations | at head `a7b8c9d0e1f2`, 24 revision files on disk |
| MCP servers | all 8 reachable |
| Temporal | worker polling `sentinel-sandbox` |
| Redis | `PONG` |
| routes | 61 API paths across 20 route modules |

Row counts: organizations 1, clusters 1, incidents 125,
incident_timeline_events 1,468, agent_audit_logs 4,975, approval_requests 61,
**remediation_gate_approvals 0**, evidence_artifacts 207, run_manifests 115,
jobs 129, slos 1, users 2, checkpoints 10,554.

Read that as "every component is **up** and the system has really been used",
which is not the same claim as "every component works as intended". The sweep
proves liveness and data; it proves no behaviour. These are the behavioural
checks still owed, cheapest first:

Three of these were **answered on 2026-09-20** against the rebuilt stack, at
no cost. They are kept here with their answers, because each one was a wrong
conclusion waiting to happen:

- **`remediation_gate_approvals` is empty but NOT vestigial.** There is
  exactly one writer, `sre_agent/approval_flow.py:460`
  (`models.RemediationGateApproval(...)`), reachable from
  `incident_remediation_workflow.py` and `api/v1/remediation_gates.py`, with
  its own migration `e4f5a6b7c8d9`. So the gate is a real, distinct path that
  **has never once fired**, while the 61 rows in `approval_requests` (44
  expired, 17 approved) came through the ordinary approval flow. The likely
  reason is that the gate belongs to autonomous remediation, which calibration
  has never let run — consistent with everything else here, but confirm it
  before relying on it. Do not settle this by writing a row.
- **The Slack bot token is a per-org database column, not an env var.**
  `organizations.slack_bot_token` is set (123 chars) with
  `slack_team_id=T0C0FK431GA`. The env sweep that reported `SLACK_BOT_TOKEN`
  "missing" was right and the inference from it would have been wrong: the
  only Slack vars in the container are app-level (`SLACK_APP_TOKEN`,
  `SLACK_OAUTH_SCOPES`, `SLACK_WAR_ROOM_CHANNEL`), because a per-tenant token
  belongs to the tenant row. This is the correct multi-tenant design, not a
  gap. Slack is the only communication channel this system has, so know where
  its credentials live before touching anything near them.
- **The litellm "fallback" is a label, not a fallback.** `actual_provider`
  is read straight from LangChain's `ls_provider` metadata in
  `sre_agent/model_accounting.py:566` (`_start`), falling back to
  `identity.constructed_provider`. Calls go through the LiteLLM-backed chat
  model, so LangChain reports `ls_provider="litellm"` while the configured
  provider is `anthropic` — and the accounting layer then records that
  difference as `fallback_from: anthropic`. Nothing actually routed away from
  Anthropic: `llm_base_url` is NULL on the one cluster row (which is
  `provider=anthropic model=claude-sonnet-5 router=true`), `LITELLM_BASE_URL`
  is unset, and the measured cost matches Anthropic pricing.
  **This is a real defect, just not a routing one**: every accounting record
  claims a fallback that did not happen, which is precisely the kind of
  untrue operator-facing claim this project exists to eliminate. Fix it by
  comparing against the *integration* name rather than the configured provider
  name. To settle it beyond doubt, assert the response's model id on one live
  call.

Both preconditions on paying for a campaign were **closed 2026-09-20, at no
cost**, by making them deterministic rather than by running the campaign:
- ~~`STATISTICAL_RECORDING` has never been set on this stack~~ — the question
  was never "is the variable set", it was "does a recording run actually
  persist `cost_usd` and a diagnosis confidence record". That is now proven in
  `tests/test_statistical_recording.py`, which imports `sre_bench` fresh
  (it reads its recording config at import) and asserts the rows:
  `test_a_complete_trace_records_the_cost`, and — importantly for where we
  actually are — `test_a_gated_trial_still_contributes_a_diagnosis_observation`,
  since every trial today stops at the human approval gate and would otherwise
  contribute nothing to the corpus that unlocks autonomy. It also pins the
  fail-closed half: an incomplete or missing trace records **no** cost and says
  why, a confidence without a graded outcome is not recorded at all, and a
  malformed or uppercase config fingerprint is refused *before* the run rather
  than at the first write, one paid incident too late.
- ~~Prove a below-support calibration artifact cannot spuriously set
  `hypothesis_confidence_calibrated`~~ — it could, and it did. Commit `2de46bb`
  ("a diagnosis artifact that blocked itself still counted as calibration")
  fixed the exact failure mode named here, and
  `tests/test_diagnosis_calibration_gate.py::test_a_self_blocked_artifact_does_not_count_as_calibration`
  holds it shut: a null threshold reads as **not** calibrated and the severity
  engine still rounds up, exactly as with no artifact at all. The paired
  `test_a_certified_artifact_suppresses_the_round_up` stops the fix from
  degenerating into "never calibrated".

So the ~$88 no longer buys the answer to either question, and no remaining
behavioural check on this list needs a paid incident. The one measured figure
is still a single gated incident at **$2.21** (smoke `b13ce2c5`, 69 model
calls) — taken *before* Phase C cut cost per model call 62%, so treat it as a
stale ceiling rather than a current estimate. Nothing has re-measured a full
incident since.

### Step 3 — wire the frontend

Start by not repeating this session's near-miss: the dashboard is **not** a
shell. It is 16 pages backed by 49 axios calls across ~25 endpoints, with
types mirrored from `backend/schemas.py` ("Types mirror backend/schemas.py
exactly so the UI renders real data" — `dashboard/lib/console.ts`, which is
presentation helpers only and fetches nothing). See the environment section
for why `grep -r "fetch("` gives the opposite impression.

Two concrete gaps:

1. **The dashboard has no restart policy.** `sre-dashboard` is the only
   service in the compose file with `RestartPolicy=no`, where `sre-agent-api`
   and `sre-postgres` are `unless-stopped`. It did **not** crash — its logs
   show `Ready in 43s`, then `HEAD /login 200`, then `Exited (255)` when the
   Codespace suspended. It simply never comes back on its own, so the UI
   looks dead after every resume and the natural conclusion ("the dashboard is
   broken") is wrong. `scripts/codespace_boot.sh` does bring it back as part
   of "ensuring platform docker-compose stack is up" — verified 2026-09-20 —
   so the gap is narrower than it first looks: the dashboard recovers only
   when someone remembers to run the boot script. Still worth one line in
   `platform/docker-compose.yaml`. It runs Next.js 16.1.6
   with Turbopack in **dev** mode, 3002→3000, from image
   `platform-dashboard:latest`.
2. **Six API modules have no dashboard caller**: `invitations`, `jobs`,
   `mission_control`, `ownership`, `tickets`, `ws_tickets`. This was matched
   fuzzily by module name, so treat it as a lead and not as proof — but `jobs`
   has 129 rows and `org_invitations` exists, so at least some real, exercised
   backend function has no UI at all. Confirm each against
   `sre_agent/api/v1/<module>.py` before building anything against it.

### Step 4 — then, and only then, benchmark

Unchanged, and still the user's call. Roughly **$88** buys calibration support
from ~40 gated incidents, which needs no autonomy because the escalation gate
reads the *diagnosis* artifact and a gated trial already emits a diagnosis
observation. The full paired ablation is the **$194 / $389 / $583** tier at
one, two and three trials per scenario.

The budget constraint is explicit and repeated: *"i am not going to spend 632
dollars"*, *"i cannot spend hundreds of dollars"* — alongside *"but i do need
to do benchmarking"*. Both halves are real. Do not launch anything paid until
the user names a stage and a budget.

## Before any benchmark run

- `SENTINEL_ABLATION_ARM` unset is production; set — even to `full` — it is a
  measurement run. `full` is the shared control, so the campaign is **80
  trials, not 120**. `--minimum-pairs` defaults to 20
  (`benchmarks/ablation_eval.py:526`).
- Always set `BENCH_INCIDENT_TIMEOUT_SEC=2700`. The 300s default is below the
  agent's 21–49 min runtime, so every trial records a non-recovery *and*
  harness cleanup clears the fault mid-investigation.
- **Memory never seeds** without `REMEDIATION_CONFIDENCE_CALIBRATION_PATH` +
  `SENTINEL_CONFIG_FINGERPRINT`: missing → `confidence.status: uncalibrated`
  → `policy.decision: blocked` → no mutation → no verification → no
  promotion. Phase 1 seeded 0 memory points for this reason, not because the
  write path is broken.
- Probe the Anthropic key before any long run; a rejected key makes the
  harness inject the fault and then fail every call for 45 minutes.

## Verification — run all of these before claiming anything is done

```bash
.venv/bin/python -m pytest -q                                  # 1975 passed, 37 known warnings
.venv/bin/python scripts/audit_runbook_controls.py             # 4 of 5 detected, 1 documented KNOWN_BLIND
.venv/bin/python scripts/audit_runbook_coverage.py \
    --corpus benchmarks/datasets/v2/runbook_corpus_snapshot.json  # 22/22
bash scripts/check_python_quality.sh                           # ruff + mypy + compileall
bash scripts/check_eval_smoke.sh                               # 45 invariants
bash scripts/check_no_static_secrets.sh                        # greps tracked files for credential shapes
```

`runbook_corpus_snapshot.json` was re-dumped from live Notion on 2026-09-20
(`scripts/dump_notion_runbook_corpus.py --cluster-id <kind-meridian>`), so it
now holds the published rewrites rather than the drafts they replaced. The
committed pre-publish snapshot scored **0/22**, which is why the command above
used to need `--proposed runbooks/meridian` to show anything. Re-dump after any
publish; grading a snapshot that Notion no longer serves grades nothing.

The secret scan runs **early** in CI's `backend-tests` job, so when it fails
nothing after it executes and the coverage/integration steps silently do not
run. If you need a credential-shaped string as test data, assemble it at
import time from parts rather than writing a literal or adding an exclusion —
see `tests/test_tracing.py` and commit `e263633` for the pattern.

Those numbers are the current baseline. A new fix should move the first one
up by exactly the number of tests you added, and move nothing else.

## When you finish a piece of work

Update `docs/ai/PROJECT_STATE.md` — **replace stale sections, do not append a
diary**; the target is 800 words. Record the defect, the evidence that proved
it, the fix, and the verification numbers. Add a `DECISIONS.md` entry only if
the decision will affect future implementation, and state its rejected
alternative and its known limit. Then say plainly what is done, what is
verified, and what is still owed from a human.
