# Scenario datasets

`v2/` is the default content-addressed benchmark dataset. `v3/` is v2 plus
the missing-data category, opt-in via `BENCH_DATASET_VERSION=v3` and **not yet
runnable** — see "v3: measuring missing data" below. `v1/` is retained
unchanged for reproducibility of older runs. `dataset.json` pins the SHA-256 of
every split — and, from schema 2, of the fixture manifest — and the loader
rejects any unrecorded edit.

Each scenario declares:

- scenario and dataset versions;
- taxonomy and risk class;
- provenance;
- alert input and fault-adapter contracts;
- expected evidence;
- allowed and forbidden action types;
- expected severity bands; and
- an independent recovery probe.

## Splits

- `train`: visible examples for development.
- `dev`: the default live benchmark split.
- `holdout`: frozen evaluation cases. The loader requires explicit local access
  and refuses to expose this split when `CI` is set.

Run another split with `BENCH_DATASET_SPLIT=train`. Local holdout evaluation
also requires `BENCH_ALLOW_HOLDOUT=true`. Do not use holdout results to tune
prompts, models, tools, thresholds, or scenario logic.

Never edit a frozen holdout file in place. Create a new dataset version,
recompute all split digests, and retain the old version for reproducibility.

## Inspecting and re-pinning

```
PYTHONPATH=benchmarks python evals/benchmarks/scenario_dataset.py --version v2
PYTHONPATH=benchmarks python evals/benchmarks/scenario_dataset.py --version v2 --repin
```

The first prints each split's scenario count and digest, failing closed on any
validation error. The second recomputes every pinned digest after a deliberate
edit and then re-loads all three splits, so it can only restore content
addressing — it can never bless a dataset that does not validate.

## Schema 2: multi-contract faults

A schema-1 scenario degrades exactly one service. Schema 2 replaces
`fault.target` / `fault.inject` / `fault.cleanup` with `fault.contracts`, a
non-empty ordered list of `{target, inject, cleanup}`, so one scenario can
degrade several services at once — a real fault plus benign noise elsewhere, or
a compound failure. The loader normalizes both schema versions to the same
internal shape, so `fault_adapter.py` and `sre_bench.py` only ever see
`contracts`.

The adapter applies contracts in the declared order and returns one lease each.
If a later contract fails, every contract already applied is unwound before the
error propagates, so a failed injection cannot leave a service degraded for the
next scenario. Cleanup restores in reverse order and attempts every lease
before reporting failures.

## Fixture capability manifest

`v2/fixtures.json` declares the fault surface the Meridian reference workload
actually exposes:

- **targets** — each service the adapter can drive, its `/admin/config` path,
  and every knob with its type, bounds, and the healthy baseline;
- **alerts** — every Prometheus rule a scenario may claim to have fired, with
  the severity and service the rule itself emits;
- **metrics** — every series a recovery probe may query.

Validation happens at load time, so a scenario that invents a fixture fails in
CI rather than on a live cluster. A scenario is rejected when it names an
undeclared target, knob, alert, or metric; uses a config path the target does
not serve; injects a value outside a knob's declared range or of the wrong
type; injects the declared healthy baseline; cleans up to a value that is not
the real baseline (which the adapter would refuse anyway); or claims a severity
or service the alert rule does not emit.

Every knob bound and baseline in the manifest was read out of the reference
workload's source, and each entry carries a `reference` pointing at it. Change
the workload and the manifest must change with it, or scenarios will keep
asserting a fault surface that no longer exists.

## Recovery probes and no-action scenarios

`require_failure_observation` arms the recovery oracle: the probe must observe
the signal leave its healthy band before recovery can be credited, otherwise
the trial reports `INVALID_SCENARIO`. Every scenario whose ground truth is a
real remediation sets it. The three sub-threshold scenarios — where the alert
fired but the measured signal never actually breached its rule, and the correct
action is none — must leave it `false`, since the probe is expected to stay
healthy throughout.

The oracle also fails closed when a query returns nothing, which constrains any
scenario that removes telemetry: **the probe must read a series that survives
the gap.** A probe aimed at the service whose exporter was just disabled
returns an empty result, and the trial reports `INVALID_SCENARIO` without ever
grading the agent. v3's missing-data scenarios are shaped around this — the
exporter goes down on one service, the fault lives on another — and
`tests/test_scenario_mix_coverage.py` asserts it.

## Known fixture constraints

These are properties of the reference workload, not of the dataset:

- **checkout-service has a ~35% baseline error ratio.** `reserve_inventory_hold`
  fails a fixed 7-in-20 hash bucket, returning 503 and incrementing
  `http_errors_total`, while `http_requests_total` counts only `/process`.
  `CheckoutHighErrorRate` (>0.10) therefore fires permanently at baseline.
  v1's `bad_deploy_checkout` probe demanded an error ratio below 0.05 and could
  never establish a healthy baseline, so that scenario would always have
  reported `INVALID_SCENARIO`. v2 sets checkout error-ratio probes at 0.45,
  above the organic baseline and below every injected value.
- **Memory only recovers on restart.** `leak_kb_per_request` appends to a
  process-lifetime buffer that is never freed, so reverting the knob does not
  lower `process_memory_bytes_simulated`. The memory scenarios require a
  checkout pod that has restarted recently enough to be under 150MB; run them
  against a freshly restarted checkout-service or they begin above their probe
  threshold.
- **`payment_failures_total` is emitted by both checkout and payment** and
  carries only a `reason` label, so probes scope it with `{job="checkout-service"}`.
  `db_query_duration_seconds` carries only a `query` label and is scoped with
  `{job="inventory-service"}`.
- **`load-generator` is not scraped by Prometheus.** It is a fault target only;
  its effects are observed through the services it drives. The bench reaches it
  at `BENCH_LOADGEN_URL` (default `http://localhost:8003`).
- **Two declared alert rules are unused.** `InventoryMemoryApproachingLimit` is
  unreachable — inventory's analytics buffer plateaus far below its 1MB
  threshold — and `PaymentServiceUnhandledErrors` fires on the same series as
  `PaymentServiceHighErrorRate`, which always trips first. Both stay declared so
  the manifest describes the real rule set rather than a convenient subset.

## What lives elsewhere

Prompt-injection, forged-approval, malicious-runbook, tool-result-spoofing,
secret-exfiltration and cross-tenant-bait cases are *not* fault-injection
scenarios — they need no cluster and assert on refusal, not recovery. They live
in `evals/benchmarks/adversarial/v1/cases.json` under the separate
`sentinel-adversarial-v1` dataset and are graded by `adversarial_eval.py`.

## Scenario-mix coverage, across all corpora

The evaluation design calls for a mix of clean/no-action, noisy, multi-fault,
missing-data, prompt-injection and cross-tenant cases. Three of those are v2
scenarios, two are measured elsewhere, and the sixth needed a dataset of its
own. "22 scenarios" on its own would imply the whole mix, so the accounting is
here:

| Required category | Where it is measured | Count |
| --- | --- | --- |
| clean / no-action | v2 `taxonomy.category = clean` (`sub_threshold`) | 3 |
| noisy | v2 `taxonomy.category = noisy` (`concurrent_benign_signal`) | 3 |
| multi-fault | v2 `taxonomy.category = multi_fault` | 3 |
| prompt-injection | A07 `indirect_injection`, `malicious_runbook`, `tool_result_spoofing` | 3 |
| cross-tenant | A07 `cross_tenant_bait`; `retrieval_eval.py` `tenant_isolation` probes | 1 + probes |
| missing-data | v3 `taxonomy.category = missing_data` | 3 |

`tests/test_scenario_mix_coverage.py` asserts this table against all three
corpora, so a category cannot quietly leave the mix.

Two notes on why the split is real rather than administrative:

* An adversarial case **cannot** be a v2 scenario. The strict loader requires
  one aggregate recovery probe returning exactly one scalar, and a
  prompt-injection case has nothing to recover — the correct outcome is that
  nothing happened. Giving it a probe would mean inventing a health signal to
  satisfy a schema, which is the failure mode the content-addressed loader
  exists to prevent.
* **Missing-data needed its own dataset version.** What the agent concludes
  when a series is absent is exercised by unit tests and fails closed in
  `recovery_oracle.py`, but until v3 no scenario measured what the *agent*
  does with absent telemetry end to end. v2's splits are frozen and SHA-256
  pinned, so closing it meant a new version — and a fixture knob that removes
  a metric source, which the workload did not have.

## v3: measuring missing data

v3 is v2's 22 scenarios plus three, one per split, in a new
`taxonomy.category = missing_data`:

| Split | Scenario | Shape |
| --- | --- | --- |
| dev | `payment_outage_with_checkout_telemetry_gap` | a real payment outage, with checkout blind beside it |
| train | `checkout_exporter_down_no_service_fault` | only the exporter is broken; the service is fine and the answer is "no remediation" |
| holdout | `inventory_slow_queries_with_checkout_blind_spot` | inventory is slow and the caller that would show user impact is unscraped |

All three set a new checkout-service knob, `metrics_enabled`. With it false,
`/metrics` answers 503 while the service keeps serving traffic and `/health`
keeps answering ok: the scrape fails, the target's `up` drops to 0, and once
the staleness window passes checkout's series stop being returned at all —
**absent, not zero.** Confusing those two is the failure the category exists to
catch, and no pre-existing knob could produce it: every other knob degrades
behaviour and leaves telemetry up.

Returning 503 rather than an empty 200 is deliberate. An empty 200 leaves `up`
at 1 and hides the gap behind a target that still looks healthy — a different
and rarer fault — whereas 503 is what a broken exporter really does and leaves
the honest signal an investigator is supposed to find.

**v3 cannot be run yet.** `metrics_enabled` is a change to the *meridian-shop*
reference workload, and until that change is pushed and the checkout-service
image rebuilt and redeployed, injecting the knob is a no-op against a running
cluster and the scenarios would silently measure nothing. The dataset validates
and is asserted by the test suite; only the live arm is blocked. `v2` remains
the `BENCH_DATASET_VERSION` default precisely so nothing picks v3 up by
accident in the meantime.
