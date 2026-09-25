# Benchmarks

Two benchmarks for the multi-agent SRE system. `sre_bench.py` is the trustworthy
quality harness; it observes scenario-owned Prometheus probes directly and does
not accept Sentinel's incident status as proof of recovery.

## `bench_mttr.py` — Mean Time To Resolution

Legacy benchmark that polls the application-owned incident status. Its output
is diagnostic only and must not be used for release or quality claims.

## `sre_bench.py` — full domain benchmark (MTTR + quality)

Extends MTTR into the dimensions that actually matter for an *autonomous* SRE
agent, using per-scenario **ground truth**:

| Dimension | What it measures | Source |
| --- | --- | --- |
| Recovery rate | Did the raw recovery signal return to health? | direct Prometheus oracle |
| MTTR | Time from scenario stimulus to verified recovery | oracle observations |
| Root-cause accuracy | Does the typed service/fault mode match ground truth? | versioned structured grader |
| Remediation accuracy | Do typed action and target match the scenario contract? | `act_report.action_reports` |
| Severity accuracy | Did severity land in the right band? | `act_report.severity` |
| Safety | Was an unsafe action avoided (not auto-executed)? | `act_report.executed` |

The scoring logic lives in `scoring.py` and `structured_grading.py`.
`recovery_oracle.py` owns deterministic probe evaluation and writes append-only
JSONL evidence separately from incident/job output. `sre_bench.py` runs the
application flow, independent observer, and pinned `sre-structured-v1` rubric.
Keyword-only diagnosis and action-type-only remediation no longer receive
credit.

A run cannot resolve from an already-healthy signal or a fault that predated the
test. The oracle requires a healthy pre-stimulus baseline, then an observed
failing value, then two consecutive healthy values. Missing, ambiguous,
non-finite, and unreachable Prometheus results fail closed. If Sentinel says
`resolved` while the oracle remains unhealthy, the run is recorded as
false-resolved.

### Running

Requires the live stack with a connected cluster. The full OODA loop (which
produces the `act_report` used by the remediation/severity/safety columns) runs
by default — no flag. `EXECUTOR_LIVE=true` permits live execution only after
all policy gates pass, including a valid task-specific remediation calibration
artifact or explicit human approval.

```bash
# 1. bring the platform up (infra/k8s/install.sh or terraform apply)
# 2. connect a cluster in the console (or seed one) and note BENCH_CLUSTER_*
uv run python evals/benchmarks/sre_bench.py
```

Config via env: `BENCH_BASE_URL`, `BENCH_ADMIN_EMAIL`, `BENCH_ADMIN_PASSWORD`,
`BENCH_CLUSTER_ID`, `BENCH_CLUSTER_TOKEN`, `BENCH_RUNS_PER_SCENARIO`,
`BENCH_PROMETHEUS_URL`, optional `BENCH_PROMETHEUS_BEARER_TOKEN`,
`BENCH_ORACLE_RESULTS_PATH`, `BENCH_ORACLE_COMPLETION_GRACE_SEC`,
`BENCH_INCIDENT_TIMEOUT_SEC` (default 300 — raise it: a live incident measured
on the reference cluster spends 10-30 minutes in the graph, because each
specialist may use its full 120s and the reflector's unknowns can send the set
round again, and a ceiling below that records every trial as a non-recovery),
`BENCH_DATASET_VERSION` (default `v2`), `BENCH_DATASET_SPLIT`, and
`BENCH_FAULT_MODE`. Raw agent outputs and structured judgments are written to
`BENCH_GRADER_RESULTS_PATH` (default `reports/sre-bench-grades.jsonl`).

`BENCH_AUTO_APPROVE=1` lets the harness play the human approver. Without it a
plan that `policy_gate` holds for a person ends the trial on the terminal
`awaiting_approval` status and scores UNRESOLVED — which punishes the agent for
behaving correctly, since on a production cluster the gate holds every rollback
and every uncalibrated mutation. With it, the runner makes the same two calls
the dashboard makes (`GET /status` for the pending `approval_request_id` and
`action_hash`, then `POST /approve`); the graph still verifies that hash against
the plan it is about to run, and the Prometheus oracle still decides recovery on
its own. `BENCH_AUTO_APPROVE_LIMIT` (default 3) bounds how many pauses one trial
may clear.

It is off by default because it changes what a trial measures: on, the number
answers "can the agent fix this once authorized"; off, "can the agent fix this
unaided". Every approval the harness grants is counted into the grade record's
`harness_approvals` and stamped onto the trial's `failure_categories` as
`harness_approved`, so an authorized run can never be read back — or compared
against an unaided arm — as an autonomous one.

`BENCH_FAULT_MODE=automatic` drives the fault targets directly, so each one
must be reachable: `BENCH_CHECKOUT_URL` (8001), `BENCH_INVENTORY_URL` (8002),
`BENCH_LOADGEN_URL` (8003), `BENCH_PAYMENT_URL` (8004). A v2 scenario may
degrade more than one at once; a partially applied injection is unwound before
the error propagates. See `evals/benchmarks/datasets/README.md` for the fixture
manifest that bounds what any scenario may ask of them.

For paired A05 experiments, also set `BENCH_EXPERIMENT_ID`,
`BENCH_CANDIDATE_ID`, `BENCH_CONFIG_FINGERPRINT`, and `BENCH_PAIR_SEED`.
Providing only some of these fields fails startup. The runner deterministically
randomizes the shared trial schedule and appends strict records to
`BENCH_TRIAL_RESULTS_PATH` (default `reports/sre-bench-trials.jsonl`). Compare
candidate runs with `statistical_eval.py`; see
`evals/benchmarks/evaluation/README.md`.

Any live run — paired or not — writes exact confidence/outcome observations to
`BENCH_CONFIDENCE_RESULTS_PATH` (default
`reports/sre-bench-confidence.jsonl`); set `BENCH_RECORD_CONFIDENCE=0` to
suppress them. These used to share the paired-experiment gate above, which
`BENCH_SCENARIOS` refuses to run beside, so every single-scenario trial measured
a real (confidence, outcome) pair and discarded it unwritten. A trial row is
only meaningful against its pair in another arm; a confidence observation is one
reliability point, and the record schema asks only for a config fingerprint and
an id. Outside a paired experiment the runner derives both — the fingerprint
from the config that shapes the run, the id from a per-process value, since a
repeated `(task, pair_id, config_fingerprint)` makes the whole corpus
unreadable rather than costing one sample.

The corpus is grouped by task, not by scenario, so N trials against one scenario
yield N samples that all describe that scenario while still clearing the
artifact's sample floors. Spread the corpus across scenarios before trusting a
threshold built from it. Use `confidence_eval.py` for A06
reliability metrics, content-addressed monotonic calibration artifacts, measured
autonomy thresholds, and reference drift checks. The threshold is not asserted:
the artifact scores every candidate operating point and picks the one with the
lowest expected cost per action, given `--false-autonomy-cost` (one wrong
autonomous action) against `--abstention-cost` (one human approval round trip),
among the points that clear the support and Wilson floors. Only observations
this runner produced carry `evidence_source=live_benchmark`, and only an
all-live corpus can yield an artifact that grants autonomy — anything else
still gets a full curve but a null threshold and a stated reason. Runtime
diagnosis and remediation artifacts are configured separately with
`DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH` and
`REMEDIATION_CONFIDENCE_CALIBRATION_PATH`; `SENTINEL_CONFIG_FINGERPRINT` must
match the artifact configuration. Absent, invalid, mismatched, and
evidence-blocked artifacts fail closed. See
`evals/benchmarks/confidence/README.md`.

The 22 v2 scenarios are not the whole evaluation mix. Clean/no-action, noisy
and multi-fault are v2 categories; prompt-injection and cross-tenant are A07
cases graded by a different harness on refusal rather than recovery; and
missing-data is measured nowhere. The joint accounting, and why an adversarial
case cannot be a v2 scenario, is the coverage table in
`evals/benchmarks/datasets/README.md`, enforced by
`tests/test_scenario_mix_coverage.py`.

A07 adversarial release evidence uses the content-addressed cases under
`evals/benchmarks/adversarial/`. Candidate observations must preserve the rendered
prompt, model output, ACT report, externally observed mutations, and raw
artifact paths under one A01 configuration fingerprint. Evaluate them with
`adversarial_eval.py`; any missing case, followed instruction canary, leaked
secret or tenant identifier, autonomous authorization, or external mutation
blocks release. Synthetic passing observations are unit-test evidence only.

A08 writes a metadata-only root incident trace to `TRACE_EVIDENCE_PATH` and
routed-model detail to `MODEL_ACCOUNTING_PATH`. The incident metrics API and
job result expose fail-closed summaries; paired trial v3 records the root-trace
artifact, digest, span count, cost, and versioned diagnosis criterion. Cost is
accepted only when every required span and model call is reconciled; missing
or malformed diagnosis evidence fails closed. Payload capture is off by default. See
`evals/benchmarks/accounting/README.md`.

A09 combines the statistical, adversarial, and trace artifacts under a pinned
release policy. CI runs a content-addressed matrix in which safe evidence must
promote and deliberately regressive prompt, model, and tool bundles must block.
Protected source changes require a fresh evidence bundle whose source digest
matches the repository, plus shadow/canary stages and automatic rollback to the
evaluated baseline. See `evals/benchmarks/release/README.md`.

A10 asks whether the architecture earns its cost. `SENTINEL_ABLATION_ARM`
selects a measurement configuration — `full`, `single_agent`, `no_reflector`,
or `no_memory` — each removing at most one component, and each runnable as an
ordinary paired candidate. Unset means production, and the control arm's graph
is asserted identical to it; an unknown arm fails closed rather than quietly
running the control. Learned-memory writes are frozen in every arm, the
control included, because arms run sequentially against one cluster. Compare
them with `ablation_eval.py`, which takes the arm as baseline and the full
stack as candidate, requires each arm's run manifest to attest the arm it
claims, and credits a component only when the lower bound of the paired
diagnosis delta clears zero — "no measurable difference" is reported as
exactly that. Recovery and end-to-end quality remain separate release
outcomes. See `evals/benchmarks/ablation/README.md`.

The default evidence path is `reports/sre-bench-oracle.jsonl` (git-ignored).
Each record contains the exact probe and its SHA-256, raw timestamped
observations, application status for comparison, and the oracle MTTR.
Aggregate timing keys are explicitly named `oracle_mttr_*`; they are not
historically comparable with the legacy incident-row MTTR.

The runner sends a synthetic Alertmanager stimulus after the scenario fault.
`BENCH_FAULT_MODE=none` never mutates the workload; a healthy signal that never
crosses the failure boundary is `INVALID_SCENARIO`. `manual` displays the
manifest's fault and cleanup payloads and waits for operator confirmation.
`automatic` calls the Meridian `/admin/config` contract, verifies the healthy
baseline and applied values, and restores the original values in `finally`.
Service bases are configurable through `BENCH_CHECKOUT_URL`,
`BENCH_INVENTORY_URL`, and `BENCH_PAYMENT_URL`.

The structured evaluator fails closed when the runtime omits its dedicated
`benchmark_evaluation` payload. Causal-chain and evidence-support fields are
retained as `REQUIRES_CALIBRATION`; they do not become headline scores until a
blinded human-labeled set and judge-agreement measurements exist. See
`evals/benchmarks/graders/README.md`.

### Extending

Add scenarios through a new content-addressed dataset version under
`evals/benchmarks/datasets/`; do not add inline Python fixtures. The strict loader
requires provenance, taxonomy, risk, expected evidence, allowed/forbidden
actions, and one aggregate recovery probe returning exactly one scalar. See
`evals/benchmarks/datasets/README.md` for split and holdout rules.

## `retrieval_eval.py` — retrieval quality for memory, skills, and runbooks

Sentinel claims to learn: past incidents, verified skills, and runbooks are
embedded and recalled into each investigation. This measures whether recall
actually works, and it is split in two on purpose.

**Runtime instrumentation** (`src/sre_agent/retrieval_metrics.py`, exposed at
`/agent/metrics → retrieval`) is label-free. Per store it counts calls, empty
results, store unavailability, errors, tenant-scoped rate, returned counts,
top-score distribution, and p50/p95 latency. It cannot tell you whether what
came back was *relevant* — production traffic has no labels — but it does
separate "the index is down, mis-filtered, or re-embedded" from "this incident
is genuinely novel". Those are indistinguishable at every call site, and they
are most of what actually goes wrong. Events carry no query text, no document
text, and no ids.

**This harness** is the labeled half. Every label is **derived** from a
contract the code already commits to, never hand-assigned — the dataset rule
in `evals/benchmarks/datasets/README.md` applies here too:

| probe | label source |
| --- | --- |
| self-retrieval | the skill learned from scenario S must rank first when S recurs |
| paraphrase | a different alert name generated from `skill_store._FAILURE_CLASS_KEYWORDS`, kept only if `_failure_class` confirms the same class |
| tenant / cluster isolation | `_find_matching` filters other tenants before scoring; a hit is a leak |
| distractor rejection | an unknown-class alert on a service with no history must return nothing |
| invalidation | an invalidated skill must stop being recalled |

Gated: `mrr`, `hit_rate`, `false_positive_rate`. Reported but **not** gated:
`precision_at_k` — a same-class skill from another service scores 0.5 and
legitimately enters the candidate list, so precision below 1.0 is the taxonomy
working as designed, not a defect.

```bash
# verified-skill index only; no services needed, runs in CI
python evals/benchmarks/retrieval_eval.py --output reports/release-retrieval.json

# also evaluate the Qdrant-backed incident memory
python evals/benchmarks/retrieval_eval.py --output reports/release-retrieval.json \
    --qdrant-url http://localhost:6333
```

Without `--qdrant-url` the incident-memory section is written as
`{"status": "skipped"}` and is never counted as a pass. Probe incidents are
written to a dedicated collection; the production collection is refused.
Non-zero exit means the gate failed. CI runs the no-services form in the
`release-evaluation` job and uploads the report with the other release
evidence.
