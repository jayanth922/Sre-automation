# Handoff prompt — continue the Sentinel/Meridian SRE agent work

Paste everything below the line into Codex as its opening instruction. It is
written to be self-contained; everything it references is in this repo.

Last updated 2026-09-19, after the four-phase pass and specialist-query
hardening. `PROJECT_STATE.md` is the concise authority.

---

You are continuing work on **Sentinel**, a multi-tenant LangGraph SRE agent
that investigates Kubernetes incidents for a demo tenant called **Meridian**,
and asks a human to approve any cluster write over Slack. The repo is at
`/Users/jayan/Downloads/Sre-automation`, branch `master`.

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

## Two decisions that belong to the user — raise them, do not action them

1. **Publish the four rewritten runbooks to Notion** (task #44). Until then
   Phase B is inert at run time.
2. **Revert `ANTHROPIC_PROMPT_CACHE_TTL` to `5m`** (task #47). It was set to
   `1h` on a projection of ~$3.00/incident; the validation run measured
   **$7.90 against a $4.77 baseline**. The split moved 2.0/27.0/71.0
   (multiplier 0.429) → 2.0/41.0/57.0 (**0.897**): the longer TTL bought more
   cache *creation*, which bills at a premium, not more reads. The code
   default is already `5m` (`sre_agent/model_router.py:581`, commit
   `1d4fe00`); the `1h` override lives in the **Codespace** `.env`, so the
   revert is an env change plus an API restart there, not a code change.

## Deferred backlog — real, unfixed, and not part of Phase C

Do not pick these up without asking; they predate the four-phase directive
and none is closed.

- **Dead operational-reflection branch** — `sre_agent/graph_builder.py`
  ~1071-1212 and ~1806-1849. The reflector's "deeper investigation" loop-back
  is collapsed to a hard `add_edge("reflector", "planner")`. The audit calls
  it a dead branch; the code comment calls it a deliberate v1 simplification.
  Both are true. Wire it or delete it — unreachable code that looks live is
  itself an honesty defect.
- **`build_supervisor_aggregate_content`** passes model text to Slack with no
  status-derived correction — the last unaudited narration surface. Reuse
  `sre_agent/narration_grounding.grounding_footnote`.
- **Task #32**: nothing cancels an in-flight investigation when an incident
  resolves externally. An unconditional cancel is wrong — a flapping alert
  would abort a legitimate diagnosis — so it needs a debounce.
- **SLO subsystem is dead**: `last_calculated 2026-09-13`, unit bug at
  `sre_agent/api/v1/slos.py:76-115`, hardcoded `None` burn rates, no latency
  SLI, not exposed as an agent tool.
- Artifact-backed context instead of stuffing context into state;
  observability semantics; no single owner of job completion.
- An MCP server that returns an **error string** still launders a failure
  into an apparent success at the boundary #35 did not cover.
- The planner sometimes emits **duplicate mutating actions** in one plan.
- A human `mark resolved` writes **no timeline event**.
- Unproven under live fire: #44 (LLM retry, deployed, never seen under a real
  529) and #41 (planner proposes only non-mutating actions — confirmed as
  behaviour, unconfirmed as a cause).
- Missing-data is measured by no corpus; v2 is frozen and SHA-pinned, so
  closing that needs a v3.

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
    --corpus benchmarks/datasets/v2/runbook_corpus_snapshot.json \
    --proposed runbooks/meridian                               # 22/22 (drop --proposed for the live corpus -> 0/22)
bash scripts/check_python_quality.sh                           # ruff + mypy + compileall
bash scripts/check_eval_smoke.sh                               # 45 invariants
bash scripts/check_no_static_secrets.sh                        # greps tracked files for credential shapes
```

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
