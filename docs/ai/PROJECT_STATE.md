# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.
Deterministic policy and durable state—not model prose—must control writes,
approvals, status transitions, and operator-facing claims.

## Current milestone
Closing the HolmesGPT-comparison gaps (canvas
`sentinel-vs-holmesgpt.canvas.tsx`). All implementable items are done: audit
redaction (#16), the Anthropic-only provider contract (#17), token-aware
context budgeting (#18), two-part retrieval quality measurement (#19),
benchmark dataset v2 at 22 scenarios (#20), cost-derived autonomy thresholds
with enforced evidence provenance (#21), the ablation harness (#22), the typed
`EvidenceRecord` contract (#23), the swarm→specialist rename in operator-facing
text (#24), striking the unmeasurable model-routing cost claim (#25), and the
cross-corpus scenario-mix accounting (#26). All of it is now **deployed and
live-verified** on the Codespace k3s cluster at revision `5956c80`. What
remains is the measurement itself: the four ablation arms have still never
been run, so the three architectural claims are measurable but not measured.

Prior milestone, deployed and live-verified: P0 #4 crash-resumable live
remediation, with stable Langfuse names, fail-closed image parity,
missed-clear reconciliation, and bounded pre-claim retries.

## Current architecture and invariants
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  policy, tenant/namespace, idempotency, and audit boundary. Human approval
  cannot override a hard policy block; `EXECUTOR_LIVE=true` fails closed
  without Temporal and an incident ID.
- Failures before an idempotency claim are retryable
  (`MutationPreDispatchError`); after it, retry-unsafe. Three Temporal
  attempts; `ERROR`/unknown is terminal for the batch. After Alertmanager
  clear the next activity returns `REFUSED/incident_resolved` and stops
  scheduling, keeping completed results in Slack/timeline output.
- Lost resolved webhooks are recovered only when the alert job's Prometheus
  rule is healthy and two snapshots ≥5 min apart show no active series;
  missing or unreachable source state leaves the incident open.
- Specialist transcripts compress into incident-owned, content-addressed
  PostgreSQL artifacts; checkpoints keep references only, and storage failure
  retains the legacy trace rather than losing evidence.
- The canonical SaaS runtime owns the successful durable-job terminal write;
  the queue worker holds the lease. API and Temporal worker share one image
  and verify the same manifest before work.
- Anthropic is the only accepted provider, enforced at Helm render time, at
  startup, and on per-tier router overrides. No surface claims unobserved
  cross-provider fallback.
- Input ceiling = `CONTEXT_WINDOW_TOKENS − CONTEXT_RESERVED_OUTPUT_TOKENS −
  margin`, enforced per iteration by a `pre_model_hook` returning
  `llm_input_messages`, so graph state keeps the full transcript. Trimming cuts
  on turn-group boundaries; an orphaned `tool_result` is a hard rejection.
- Retrieval is measured in two halves that never merge: label-free call shape
  from production (`/agent/metrics → retrieval`) and labeled offline ranking
  (`benchmarks/retrieval_eval.py`). `mrr`, `hit_rate`, `false_positive_rate`
  gate; an unrunnable store reports `skipped`, never a pass.
- `evidence_contract.py` is the single definition of what severity may count:
  the metric allowlist, each metric's type, and its aliases. `EvidenceRecord`
  requires an agent and a tool, so an unattributed number is not evidence, and
  an uncoercible value becomes `None` (UNKNOWN) rather than a calm zero. The
  walker, the artifact reader and the alert-label path all read that one
  registry; pre-contract checkpoint records are split back apart, not dropped.
  `EvidenceRecord` (observation), `EvidenceLink` (severity's decision input)
  and `EvidenceReference` (model-authored citation) stay separate on purpose.
- The graph node id `investigation_swarm` is frozen — it is written into
  LangGraph checkpoints and Langfuse span names — but no operator-facing string
  calls the specialist split a swarm; a test enforces that.
- Benchmark scenarios are data, never inline Python. `dataset.json` pins a
  SHA-256 per split and for `fixtures.json`, which declares the fault surface
  the workload really exposes; anything a scenario names but the manifest does
  not declare is a load-time `DatasetError`.
- The autonomy threshold is selected, not asserted: the cheapest eligible
  operating point under a recorded `cost_model` and `selection_rule`. Support
  and Wilson floors constrain eligibility but no longer select. Only
  `sre_bench.py` emits `evidence_source=live_benchmark` and only an all-live
  corpus yields a threshold; anything else gets a curve, a null threshold, and
  a stated `autonomy_blocked_reason` reaching operators via `ActReport`.
  `load_calibration_artifact` recomputes everything, so a hand edit fails.
- `SENTINEL_ABLATION_ARM` unset is production; set (even to `full`) it is a
  measurement run. Four arms remove at most one component each, an unknown arm
  raises `AblationError` rather than degrading to the control, and the arm is
  written into the manifest's `runtime` section — one of the four sections the
  A01 fingerprint hashes — so two arms are structurally incomparable. Every
  arm freezes learned-memory writes, the control included, because arms run
  sequentially against one cluster.
- `benchmarks/ablation_eval.py` takes the arm as baseline and the full stack as
  candidate, so paired deltas read "full minus arm". It requires each arm's run
  manifest to re-hash to the fingerprint on that arm's trials, closing the hole
  that `BENCH_CONFIG_FINGERPRINT` is operator-declared. A component is credited
  only when the lower CI bound clears zero; an interval containing zero is
  `NOT_DEMONSTRATED`, annotated when the evidence was too thin to be a null.

## Completed or verified work
- Crash-resumption proven live: a workflow that lost its worker resumed after
  alert clear with one `EXECUTED`, one `REFUSED(incident_resolved)`, one audit,
  no repeated mutation. A CAS blocks late-webhook/reconciler races.
- Langfuse trace `1814f33e…`: 142 observations, zero generic `agent` names, all
  31 generations carrying model and usage metadata.
- Dataset v2: 22 scenarios (12/6/4) over 4 fault targets, all injecting and
  fully restoring in test. v1's `bad_deploy_checkout` probe could never have
  gone healthy; v2's thresholds sit above checkout's organic error ratio.
- Confidence calibration is empirical end to end: on a graded 300-sample
  corpus `false_autonomy_cost=2` selects threshold 0.597 at 80% coverage while
  `=200` selects 0.969 at 20%. Four artifact tamper paths are closed.
- Ablation arms verified by graph shape: control and unset are node- and
  edge-identical; `no_memory` differs only behaviourally; `no_reflector` drops
  ORIENT and its re-investigation loop; `single_agent` keeps the reflector and
  the report writer. Two strawmen were avoided — the planner now reads the
  specialists' findings directly when ORIENT is gone (still wrapped as
  untrusted), and the single investigator writes where the reflector reads.
- Qdrant 1.19.1, PostgreSQL 15.19, Redis 7.4.11, Temporal CLI 1.8.3, Anthropic
  1.7.2 digest-pinned; Temporal SDK 1.32.0.
- **Live fire, 2026-09-18, Codespace k3s at `5956c80`.** Runtime parity passed
  on both containers (`fingerprint=e7af89b2…`, 155 files). One webhook alert ran
  the whole loop to `awaiting_approval` in ~35 min: specialists → reflector
  (confidence 0.72, 8 discrepancies) → two bounded re-investigation rounds →
  planner → approval gate. Its run manifest is `comparable: true` with no
  reasons, `provenance.code_sha=5956c809…`, `working_tree_dirty: false`,
  Anthropic-only routes, and `runtime.ablation_arm="full"` with
  `ablation_experiment: false` — unset really does report as production. The
  diagnosis measured 82.5% 5xx itself and refused to treat the alert text's
  "revision 5956c80" as a real revision, which is the untrusted-evidence
  boundary working on live input.

## Active problem
No benchmark campaign has produced an `act_report` yet, so the HolmesGPT
comparison still rests on design description and there is no real confidence
calibration artifact.

**#40, fixed `71041b9`, not yet deployed — the planner discarded every plan
over a newline.** `RemediationPlan.actions` was already wired to
`_decode_json_container`, and the deployed container coerces a clean
stringified list correctly, yet on 2026-09-19 (incident `bc5c48b7`) planning
still failed with `Input should be a valid list` — 1 of 1 invocations in 24h,
the same signature as the four-for-four episode on 2026-09-14. `json.loads`
is strict about control characters, and models leave literal newlines inside
prose fields like `safety_check`; the string is structurally sound but
unparseable, so the decoder handed it back and Pydantic raised the real
error. Fixed by retrying with `strict=False`, and by logging why a decode was
abandoned — Pydantic truncates the middle of the value, so the log showed a
string well-formed at both ends with no reason attached, which is why this
survived two incidents. Third instance of the recurring root cause — the
suite builds shapes production never sends.

The rate is intermittent, not universal: measured 1 failure in 2 planner
invocations on 2026-09-19 (`bc5c48b7` failed, the `checkout_high_latency`
pilot succeeded with a 3-action low-risk plan). So this degrades Phase 1
rather than blocking it — each lost plan falls back to `escalate
manual_review`, which mutates nothing, so `EXECUTOR_LIVE` never engages, no
objective verification exists and `eligible_for_success` stays false for that
scenario. At the observed rate roughly half the seeding yield is lost, which
is why the fix ships before the remaining eleven scenarios are bought.

`ACT_PHASE_ENABLED=true` is now set in the Codespace `.env` (backup:
`.env.bak-phase0`) and confirmed wired at runtime — `graph_builder.py:2123`
logs the investigate ↔ reflect → planner → aggregate → approval_gate →
act_gate chain. `EXECUTOR_LIVE` and `TEMPORAL_ENABLED` are `true`.

Phase 0 is **done** (two runs: `5cc643c5` crashed, `281b8110` clean). It
answered both questions and surfaced one structural defect:

1. **Cost (answered).** `5cc643c5`: 111 calls, **$4.77**, 20.8 min, 3.84M
   tokens — 2.0% uncached input, 27.0% cache creation, 71.0% cache read.
   Effective input multiplier **0.429** (~57% off all-uncached), *not* the
   ~70% once projected: cache creation bills at a premium. `281b8110` ran 110
   calls / 21.1 min but its total is `null` — one span failed with unknown
   tokens and the accounting refuses to fabricate a total.
2. **Memory writes (answered: no, and not because the write path is broken).**
   Both runs ended `requires_approval`, so `outcome_class` was `dry_run` /
   `incomplete` and `agent_runtime.py:1908` skipped promotion. Stores
   unchanged across both: `skills.json` 5 @ `success_count=1`,
   `sre_skills_v1` 5, `sre_incidents_v2` 0. The *retrieval* half works live —
   `SkillStore: semantic recall enabled` and 3 learned skills proposed.
3. **Root cause — autonomous ACT was unreachable in production (task #33,
   now fixed at `8c7b4c0`).** `compute_urgency_score` needs `slo_burn_rate`,
   `saturation`, or `error_rate_slope`. Production supplied none, so urgency
   was always `None` → severity always `UNKNOWN` → `is_low_severity` always
   `False` → **no plan could ever run unattended, for any incident.** The
   earlier attribution of `5cc643c5`'s UNKNOWN to "the fault was cleared" was
   wrong: `281b8110` kept its fault injected and still went UNKNOWN. Tests
   missed it because they inject all three metrics by hand — they exercised
   the scoring maths, never the pipeline's ability to supply its inputs.

   It was **two** defects, not one, and fixing only the first would have left
   autonomy just as unreachable:
   - *Slot poisoning.* `_walk_metrics` claimed a metric slot on `key_str not
     in found` regardless of whether the value coerced, so the MCP's wrapper
     dict claimed `saturation` and hid the number below it.
   - *Unreachable value.* The real payload from
     `prometheus_real/server.py:392` is not `{"query":…,"value":0.42}` but a
     capped Prometheus instant-vector envelope three layers deep. Recovering
     it needed `act_phase._prometheus_scalar`, which unwraps **only**
     unambiguous cases: zero series is not a measurement and several series
     are several measurements, so both stay `None` rather than having one
     picked and called evidence.

   The fix does not rely on the model choosing to call an MCP tool. A new
   `severity_telemetry` graph node (between `aggregate` and
   `approval_prepare`) measures the inputs deterministically from the
   cluster's own Prometheus and writes `metadata["severity_telemetry"]`,
   which `extract_incident_signals` absorbs at **highest** precedence — it is
   the only source whose query provenance the platform itself authored. Two
   optional per-cluster profile fields drive it, `saturation_query` (with a
   `$service` placeholder, so the reading describes the service on fire
   rather than a diluting cluster-wide average) and `slo_target`. `cpu_query`
   is deliberately **not** reused: it is customer-authored with no declared
   unit, and guessing 100× low would understate urgency and make a plan
   *more* likely to run unattended. A cluster setting neither field keeps
   today's behaviour exactly (unmeasured → UNKNOWN → human approval), which
   is the safe direction to fail; a *malformed* value raises
   `MetricsProfileMalformed` instead of being silently dropped.

   A third defect of the same family surfaced when the node first ran live
   (`d168d5f`): it read `alert_context` behind an `isinstance(alert, dict)`
   guard, but at runtime that key holds an `AlertContext` model — only tests
   and resumed checkpoints carry a dict. Labels came back empty, so the node
   executed on every incident and measured nothing. **The recurring lesson is
   that this suite hand-builds shapes production never sends**; the node now
   uses `act_phase._get` (dict key *or* object attribute), is covered by a
   test parametrized over both shapes, and logs when it measures nothing so
   silence is never again indistinguishable from the node not running.

Also open: **task #32**, nothing cancels an in-flight investigation when an
incident resolves externally (`durable_jobs.request_cancel` has zero callers;
the claim at `agent_runtime.py:1338` covers only the start-time backstop). An
unconditional cancel is wrong — a flapping alert would abort a legitimate
diagnosis — so it needs a debounce or verified-recovery condition.

Reflector cost is now **bounded, not unquantified**: ~110 calls and ~21 min
per incident, both runs, ≈$4.8. Convergence improved run-over-run
(0.72→0.86→0.72 vs 0.62→0.62→0.88, discrepancies 7→6→7 vs 6→6→5).

Deferred, unrelated: digest-pin the Helm Temporal server image.

## Relevant files
- Ablation: `sre_agent/ablation.py` (arms), `benchmarks/ablation_eval.py`
  (harness), `benchmarks/ablation/README.md` (operator runbook),
  `sre_agent/graph_builder.py` (per-arm wiring),
  `sre_agent/config/agent_config.yaml` + `prompts/single_agent_prompt.txt`
  (the single-investigator baseline), and the write gates in `act_phase.py`,
  `supervisor.py`, `agent_runtime.py`.
- Paired statistics: `benchmarks/sre_bench.py` (`BENCH_CANDIDATE_ID`,
  `BENCH_CONFIG_FINGERPRINT`), `benchmarks/statistical_eval.py`.
- Corpus: `benchmarks/datasets/v2/` (+ `fixtures.json`), `scenario_dataset.py`,
  `fault_adapter.py`.
- Confidence: `sre_agent/confidence_calibration.py`,
  `benchmarks/confidence_eval.py`, `benchmarks/confidence/{v1,v2}/`, and the
  loaders behind `DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH` /
  `REMEDIATION_CONFIDENCE_CALIBRATION_PATH`.
- Retrieval: `sre_agent/retrieval_metrics.py`, `skill_store.py`,
  `memory_store.py`, `runbook_index.py`, `benchmarks/retrieval_eval.py`.
- Evidence typing: `sre_agent/evidence_contract.py`, its readers in
  `act_phase.py`, and `tests/test_evidence_contract.py`.

## Verification commands and latest results
**There are two complete platform stacks, and only one is live.** The Mac runs
`sre-agent-api` / `sre-postgres` / `sre-redis` / `sre-qdrant` / `sre-temporal`
/ `sre-temporal-worker`, and the Codespace runs a second full stack of the
same services (plus `sre-dashboard`). **The Codespace stack is the one under
test** — Alertmanager's webhook points at it and `BENCH_BASE_URL` resolves to
it — so deploys, API writes, and log greps all belong there; the Mac stack is
a separate dev copy whose database receives none of it. Assuming a single
stack produced a wrong conclusion once already (a cluster-profile PATCH issued
against the Codespace was checked for on the Mac, found absent, and misread as
a failed write). Forwarding the Codespace's port 8080 to the Mac also fails,
because the Mac's own `sre-agent-api` already holds it.

- `uv run pytest tests/ -q`: **1,733 passed** in ~31s. (`test_live_remediation_temporal_workflow` can fail
  on a Temporal test-server port bind when other jobs hold the port; it passes
  run alone.)
- `uv run python benchmarks/retrieval_eval.py --output reports/release-retrieval.json`:
  PASS — 25 probes on v2, mrr/hit_rate/nDCG 1.0, false_positive_rate 0.0,
  memory store `skipped` without `--qdrant-url`.
- `PYTHONPATH=benchmarks .venv/bin/python benchmarks/scenario_dataset.py --version v2`:
  12/6/4 = 22 scenarios, digests matching; `--repin` idempotent.
- `PYTHONPATH=. .venv/bin/python benchmarks/make_release_fixtures.py --check`: exit 0.
- `bash scripts/check_python_quality.sh`: passed.
- Deployment-template gate and secret scan: passed;
  `helm template --set llm.provider=gemini` fails at render time as intended.
- Live, in the Codespace: `python3 scripts/check_runtime_parity.py` (it runs on
  the Codespace host and `docker exec`s in; `scripts/` is not in the image) →
  `code_sha=dff11fb2fdcf… fingerprint=e5d73be36eaa… files=155`. Deploy with
  `bash scripts/deploy_agent_runtimes.sh`, which exports `SENTINEL_CODE_SHA`
  from git HEAD — a bare `docker compose build` bakes `code_sha=unknown` into
  the image and the manifests lose their revision. Bring the stack up only as
  `cd platform && docker compose --env-file ../.env up -d`; from the repo root
  `${POSTGRES_*}` interpolate empty and asyncpg fails as user "root".
- Live cost accounting, same revision: one real routed `narration` call
  recorded `cost_usd=3.4e-05`, `cost_source="derived"`, rates attached, and no
  `cost_unavailable` reason.

## Known blockers or risks
- Never stage the untracked secret backup `.env.local-backup-20260910`; the
  `.gitignore` `.env` pattern does not match it. Always use explicit paths in
  `git add`.
- The Meridian checkout baseline is restored **in the working tree only**.
  Two commits on `origin/master` of `jayanth922/meridian-shop` —
  `4ed89b2` (hash-slot 503s) and `7a6e223` (`int(order_id[-1])` on
  letter-suffixed order ids) — held checkout at an 84% 5xx ratio, above the
  0.45 `checkout_error_ratio` probe threshold, so two v2 dev scenarios could
  not establish the healthy baseline the oracle demands. The Codespace copy of
  `services/checkout-service/app.py` is checked out at `c6725b8` and the image
  rebuilt; measured ratio is now 0.0 at 2.3 rps. Nothing was pushed. See
  `/workspaces/meridian-shop-deploy/.local-baseline-restore.md`.
  `scripts/watch_meridian_deploy.sh` does `git reset --hard` when
  origin/master moves and would silently discard the restore.
- The evaluation mix is five of six categories. **Missing-data is measured by
  no corpus**: no scenario tests what the agent concludes from absent
  telemetry. v2's splits are frozen and SHA-256 pinned, so closing it needs a
  v3 with a fixture knob that removes a metric source. Declared in the
  coverage table in `benchmarks/datasets/README.md` and pinned by
  `tests/test_scenario_mix_coverage.py`, which fails if the declaration is
  removed without a dataset that measures it.
- No real calibration artifact exists and none can be built without a paired
  A05 `sre_bench.py` run against a live cluster; synthetic evidence is refused
  at load time. Until then remediation autonomy stays fail-closed on human
  approval — the intended state, not a gap. The ablation arms have the same
  dependency.
- v2 has never run against a live cluster; the adapter round trip is proven
  only against manifest-derived fakes. Memory scenarios need a checkout pod
  restarted recently enough to sit under 150MB (`leak_kb_per_request` never
  frees its buffer).
- **`docker exec … python` is not the agent.** The agent is `uv run`, i.e.
  `/app/.venv/bin/python`; the container's bare `python` is
  `/usr/local/bin/python` and has none of the project's dependencies. Probing
  with it reports `qdrant-client not installed` and a keyword-only skill
  store, which is indistinguishable from a real degradation — it produced a
  false 3/6 coverage reading and a false "semantic recall is dead in
  production" finding, both since retracted. The true state is semantic,
  6/6. Always `docker exec sre-agent-api /app/.venv/bin/python`, and prefer
  `ablation_coverage.py --expect-retrieval-path semantic`, which exits 2 on
  the wrong stack; the artifact also records the interpreter that produced it.
- **Incident recall is empty, by design, until ACT runs.** `sre_incidents_v2`
  holds zero points because `agent_runtime.py:1852` gates `store_incident` on
  `eligibility.eligible_for_success`, which needs an `act_report` with
  objective verification, and `ACT_PHASE_ENABLED` has never been on. Not a
  bug. It does mean the learned-memory arm currently measures only its
  verified-skill half, and that the corpus must be populated by a
  **non-experiment** run before the experiment freezes writes for every arm.
- `CONTEXT_WINDOW_TOKENS` is operator-declared; a model with a window under
  200k will under-reserve unless it is set.
- tiktoken fetches its BPE file on first use; an air-gapped image without that
  cache silently degrades to the character heuristic.

## Next bounded task
**Phase 1** (train split, production mode, to seed memory legitimately), then
**Phase 2** (dev split `full` + ablation identifiers), then **Phase 3** (the
three counterfactual arms).

Task #33 no longer blocks them. Of its three candidate fixes, option 3
(relaxing the all-three-missing early return) was **rejected** — it weakens a
safety property to buy a symptom — and options 1+2 were implemented instead,
with per-cluster explicit configuration and a fail-safe default. Live cluster
`bcbd9577-…` is configured and measuring (`saturation_query` scoped by
`$service`, `slo_target=0.99`).

Verification, and what it does and does not cover. Deployed at `56163c3`
(parity `f73f0dce…`, 156 files). Inside the running container, against the
real cluster row and real Prometheus, a production-shaped `AlertContext`
drives the node to `SeverityTelemetry: measured … for
service=inventory-service`, and the same incident classifies
`UNKNOWN`/`autonomous=False` without the telemetry versus
`SEV3`/`autonomous=True` with it. Graph placement was proven separately by
live incident `0d6fa6eb` at the prior revision, whose logs show `processing
node: severity_telemetry` firing between the planner and the ACT write.
Closed in one pass on 2026-09-19 during the Phase 1 pilot: organic incident
`bc5c48b7` (`[api-gateway] PodOOMKilled`) logged `SeverityTelemetry: measured
error_rate=0.0012, error_rate_slope=0.00024, saturation=0.207,
slo_breached=False, slo_burn_rate=0.122 for service=api-gateway`, and the
engine then scored `impact=0.02 × urgency=0.06 → SEV4; escalated to SEV3`
instead of UNKNOWN. Measurement, classification and graph placement are now
verified together on a real incident, not separately.

Note SEV3 rather than SEV4: a separate, intentional escalation fires because
diagnosis confidence is uncalibrated. SEV3 is still inside the autonomous
band, so it does not block ACT, but it means the calibration artifact is what
now governs how far severity can fall.

Baseline to diff every memory
claim against, taken 2026-09-19 and still true after two runs: `skills.json`
5 skills each at `success_count=1`, Qdrant `sre_skills_v1` 5 points,
`sre_incidents_v2` **0 points**. The corpus already holds a
`latency-inventory-service` skill, so on `inventory_slow_queries` the correct
outcome is that skill incremented to 2 — not a sixth written.

Operational notes for whoever runs these: `BENCH_SCENARIOS=<name>` narrows a
run to one scenario without buying a split, and is refused alongside the
statistical env on purpose (`sre_bench.py:95` enables statistical recording
if *any* of `BENCH_EXPERIMENT_ID` / `BENCH_CANDIDATE_ID` /
`BENCH_CONFIG_FINGERPRINT` / `BENCH_PAIR_SEED` is set, then demands all
four). Read cost from `/api/v1/incidents/{id}/agent-metrics` only after the
job leaves `running`; a single failed span nulls the whole total by design.
The Codespace tree is now clean and at `8c7b4c0`, matching local content
exactly (same HEAD tree sha). It got there by patch — `git format-patch` on
the Mac, `git am` in the Codespace — not by pushing, since pushing was not
authorized. `deploy_agent_runtimes.sh` refuses a dirty tracked tree, so keep
it clean before any redeploy. Note `uv` is not on the non-interactive SSH
`PATH`; prefix with `export PATH="$HOME/.local/bin:$PATH"`.

Then the four ablation arms. Everything they need is in place: Codespace
`cuddly-winner-659v67gv695hrxjw`, k3s up, Meridian healthy, and `~/bench.env`
holding every `BENCH_*` value (base URL, the `bench-runner@example.com`
service account created through the sanctioned invitation flow, cluster id
`bcbd9577-…`, cluster token, and the four fault service URLs). Those URLs are
now `localhost`, not a node IP: Docker-in-Docker storage does not survive a
Codespace stop/resume, so k3s restarts with a new node IP and any baked-in
address goes stale. `scripts/codespace_boot.sh` repoints the Alertmanager
webhook but does **not** touch `~/bench.env`.

1. Confirm `ACT_PHASE_ENABLED=true` in the Codespace `.env`, then recreate the
   API and worker with `bash scripts/deploy_agent_runtimes.sh`. **Not** a bare
   `docker compose up -d`: the image now has a real `SENTINEL_CODE_SHA` baked
   in, compose defaults the runtime value to `unknown`, and preflight then
   fails closed with `runtime code revision mismatch: image=dff11fb…
   deployment=unknown` (verified). The script exports it from git HEAD, and
   refuses to run against a dirty tracked working tree.
2. `export BENCH_INCIDENT_TIMEOUT_SEC=2700` (a run needs 10-30 min),
   `BENCH_RUNS_PER_SCENARIO=1`, `BENCH_DATASET_SPLIT=dev`,
   `BENCH_FAULT_MODE=automatic`.
3. The four arms back to back per `benchmarks/ablation/README.md` — one
   `BENCH_EXPERIMENT_ID`, one `BENCH_PAIR_SEED`, one
   `BENCH_TRIAL_RESULTS_PATH`, distinct `BENCH_CANDIDATE_ID` and
   `BENCH_CONFIG_FINGERPRINT`. Budget ~3h per arm.
4. Capture each arm's manifest from
   `/api/v1/clusters/$BENCH_CLUSTER_ID/jobs/$JOB_ID/manifest` (verified
   working: `comparable: true`, arm in `runtime`), then `ablation_eval.py`
   with `--memory-coverage`.

Run `benchmarks/ablation_coverage.py` **as `/app/.venv/bin/python` in the
agent container** before spending anything — `ablation_eval.py` records an
`insufficient_evidence` entry for `no_memory` without it. Last measured there:
`COVERED: all 6`, `retrieval_path: semantic`, 3 skills retrieved per scenario
from the 5-skill corpus. Incident recall is still provably inert (zero points
in `sre_incidents_v2`), so the arm measures its verified-skill half only.
Populating it means one **non-experiment** pass over the train split with
`ACT_PHASE_ENABLED=true` — legitimate, since dev stays unseen; seeding from
dev would be leakage.

Six pairs per arm will likely report `NOT_DEMONSTRATED` with a non-empty
`insufficient_evidence` list. Report that as ignorance, not a null.

**API budget.** The first live incident made 123 model calls for 3.37M input
and 114K output tokens. Repricing those tokens gives **$8.32**, but treat that
as an *upper bound, not a measurement*: it prices all input at the uncached
rate although the static prefix was already cached, so the true spend was
nearer $6.80. Sonnet-5 specialists are ~91% of it — 111 calls averaging ~30K
input each, because every ReAct turn re-sends the growing history. On that
basis one arm was ~$50 and the four together ~$140, since `no_reflector` (~$16,
no re-investigation rounds) and `single_agent` (~$22) are much cheaper than
`full` and `no_memory`.

#29 pulled the lever this block used to flag as future work: the conversation
body is now tagged `cache_control` too, at a 5m TTL (see DECISIONS.md). The
mechanism is verified live — a third turn read back its entire 18K-token
predecessor — but the *magnitude* on a real investigation is a projection, not
yet a measurement: a 14-turn loop should see roughly 70% off its input cost,
putting an incident near $3 and the four arms near $65. **Keep the budget at
$75 for one arm and $200 for the set** until an arm measures it. It now can:
every record carries `tokens.cache_read` and `tokens.cache_creation`, so the
first arm's own accounting reports the real saving.
