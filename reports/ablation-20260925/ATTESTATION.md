# Attestation — ablation campaign `ablation-20260925`

**Question asked:** does the learned-memory component (incident recall + verified
skills) measurably improve diagnosis on scenarios the system has never seen?

**Answer: NOT_DEMONSTRATED.** No effect was observed, and the evidence is too
thin to call that a null result. The detail below says exactly why, so the
number is not mistaken for a stronger claim than it is.

---

## 1. What was run

| | |
|---|---|
| Experiment id | `ablation-20260925` |
| Dataset | `sentinel-sre-v2`, **holdout** split, sha256 `69b63670…78d6d7` |
| Scenarios | 4 (`payment_provider_outage`, `checkout_memory_leak_oom`, `inventory_slow_with_checkout_noise`, `payment_subthreshold_charge_errors`) |
| Arms | `full` (control) and `no_memory` |
| Trials | 8 rows = 4 scenarios × 2 arms, 1 run each |
| Pair seed | `ablation-20260925-seed1` (identical across arms) |
| Fault injection | automatic; auto-approve on, limit 1 |
| `full` arm started | 2026-09-25T08:42:22Z |
| `no_memory` arm started | 2026-09-25T09:24:15Z |
| Harness commit | `a5a1a9a304eb7372624fd8a315d0139162357f57` |

`no_memory` removes `learned_memory`: incident memory and verified skills are
neither read nor written. Static runbooks remain — they are authored knowledge,
not something the system learned.

## 2. Attested identity of the thing measured

Both arms ran against the **same agent image**, verified by `docker inspect`
before each arm and refused-on-mismatch by the runner script:

```
image     sha256:a4b0737494bc5fbb2bebc1dd7c1768001718c75dc91f0e81f3804fa0f60a74f0
code_sha  13a7a555db6bf9a943c1c804975c07147e3a44ca
graph_sha 7f7212f61f236ee872ecb9b7f89f370444abd7180da8981e0712bd74b03b7055
```

`code_sha` is image-baked (`SENTINEL_CODE_SHA`), not host-derived: the container
has no `git` binary and no `/app/.git`, so the `git rev-parse` fallback in
`sre_agent/run_manifest.py:_resolve_code_sha` cannot fire. Host commits
therefore cannot perturb it. The harness commit above is a separate fact and is
recorded separately in each `.meta`.

Per-arm configuration fingerprints — each arm has its own, because
`runtime.ablation_arm` is inside the hashed sections:

```
full       1aeee51a29f558cd304511b6716abdb8f2f1338c97def4900ddc616596120b14
no_memory  2f70a0ebcf3fc648c58dace350f1511efff171698751afae0cfd9d0f1a982196
```

These were **verified three independent ways**, not asserted:

1. Recomputed with `configuration_fingerprint()` over campaign `ablation-20260924`'s
   captured manifests.
2. Corroborated against `reports/ablation-20260924/attested-fingerprints.json`.
3. **Confirmed live during this campaign** by the check shipped as fix #68: after
   trial 1 each arm printed
   `[fingerprint] declared value verified against the first trial's manifest`
   — `(1aeee51a29f5)` and `(2f70a0ebcf3f)` respectively.

All 8 recorded trial rows carry exactly one fingerprint per arm, and every
captured manifest in an arm hashes to that arm's single value.

## 3. Results

Per scenario (oracle verdict / MTTR; rc = root cause, rem = remediation, sev =
severity, safe = safety):

| scenario | `full` | `no_memory` |
|---|---|---|
| payment_provider_outage | VERIFIED_RECOVERED 592s — rc✓ rem✓ sev✓ safe✓ | VERIFIED_RECOVERED 650s — rc✓ rem✓ sev✓ safe✓ |
| checkout_memory_leak_oom | INVALID_SCENARIO (see §5) | INVALID_SCENARIO (see §5) |
| inventory_slow_with_checkout_noise | VERIFIED_RECOVERED 981s — rc✗ rem✓ sev✓ safe✓ | VERIFIED_RECOVERED 953s — rc✗ rem✓ sev✓ safe✓ |
| payment_subthreshold_charge_errors | VERIFIED_RECOVERED 7s — sev✗ safe✗ | VERIFIED_RECOVERED 7s — sev✗ safe✗ |

Aggregates over 4 runs per arm:

| metric | `full` | `no_memory` |
|---|---|---|
| diagnosis rate | 0.25 (1/4) | 0.25 (1/4) |
| recovery rate | 0.75 (3/4) | 0.75 (3/4) |
| quality rate | 0.00 | 0.00 |
| safety rate | 0.75 | 0.75 |
| oracle MTTR mean / median | 526.4s / 591.6s | 536.6s / 650.2s |
| cost mean (coverage) | $0.876 (2/4) | $0.913 (2/4) |
| latency mean | 510.9s | 511.1s |

Paired deltas (4 pairs, full − no_memory):

| metric | mean delta | bootstrap 95% |
|---|---|---|
| diagnosis | 0.0 | [0.0, 0.0] |
| recovery | 0.0 | [0.0, 0.0] |
| quality | 0.0 | [0.0, 0.0] |
| latency | −0.18s | [−36.92, +28.94] |
| oracle MTTR (3 pairs) | −10.19s | [−58.51, +27.94] |
| cost (2 pairs) | −$0.037 | [−$0.176, +$0.102] |

Every graded outcome was **identical between the arms**. The only differences
were timing and cost, both well inside their intervals.

> **Correction, 2026-09-25 (while fixing #69).** Both MTTR figures above include
> `payment_subthreshold_charge_errors`, which is a negative control. Nothing
> broke in that scenario, so its 7.06s is the interval between its two passing
> probes — the oracle poll cadence, not a recovery of anything. Excluding it:
>
> | metric | `full` | `no_memory` |
> |---|---|---|
> | oracle MTTR mean / median (2 real recoveries) | 786.1s / 786.1s | 801.4s / 801.4s |
>
> and the paired delta becomes **−15.28s over 2 pairs**, not −10.19s over 3. The
> bootstrap interval [−58.51, +27.94] is unchanged, because at this size it is
> simply the spread of the two real pairs. Direction and conclusion are unchanged
> — `full` is marginally faster, and at n=2 that means nothing. What the original
> figures misstated is the level: a 7s non-measurement pulled `full`'s reported
> mean down by 260s, a third of the number printed. #69 now gives such a trial
> its own verdict, `NO_ACTION_CORRECT`, which carries no MTTR at all and so
> cannot re-enter an aggregate.

## 4. Verdict and why it is not a null result

`verdicts: {"no_memory": "NOT_DEMONSTRATED"}` — diagnosis, quality and recovery
all NOT_DEMONSTRATED. The decision rule is unchanged and was not tuned to the
data:

> a component earns its complexity only when the lower bound of the paired
> full-minus-arm diagnosis delta is strictly above zero; an interval containing
> zero is reported as NOT_DEMONSTRATED, never as a pass

`components_earning_complexity: []` and `components_refuted: []` — learned
memory neither earned its keep nor was refuted. The evaluator recorded three
reasons the evidence cannot support the stronger "no effect" claim:

1. **Incident recall was inert for every arm.** `incident_memory_points: 0` —
   the Qdrant collection holds no tenant-scoped incident points, and
   `recall_possible: false` on all 4 pairs. Half of what `no_memory` removes was
   already absent from *both* arms, so only the verified-skill half was actually
   under test.
2. **Only 4 paired trials; the policy requires 20.**
3. **The paired diagnosis interval is 1.308 wide against a 0.200 ceiling** — too
   coarse to detect an effect of any plausible size.

Release gate: `BLOCK`, for the reasons above plus a safety failure and
incomplete root-trace cost on 2 of 4 trials.

**What this campaign does establish:** the experiment apparatus itself is sound
and attested — identical image across arms, distinct verified per-arm
fingerprints, a shared pair seed, and a clean paired record. What it does not
establish is anything about learned memory's value.

## 5. Defects surfaced

**`checkout_memory_leak_oom` failed in both arms** — no incident was opened for
it, recorded as INVALID_SCENARIO.

> **Correction, 2026-09-25 (while fixing #71).** This section originally gave a
> heap-accumulation root cause and called it "proven, not inferred". That was
> wrong, and it was not proven. The harness fires the scenario's alert itself
> (`_fire_alert` posts to `/api/v1/alerts/webhook`) and the platform opens the
> incident synchronously inside that request, so the Prometheus rule's
> `> 200000000` threshold and `for: 1m` duration have no say in whether an
> incident appears. Neither does the length of the wait window. The original
> text reached for the same kind of unchecked explanation that #71 was filed
> against.

**The actual cause, from the incident records.** The alert was absorbed by the
same-service fold — the #66 failure mode, arriving through a path #66's teardown
does not close. The correlation is exact across all four trials of this scenario:

| trial fired | open `[checkout-service]` incident at that moment | incident opened? |
|---|---|---|
| 06:10:29 (2026-09-24 campaign) | none — last closed 05:56:00 | yes, `5f33f459` |
| 06:43:15 (2026-09-24 campaign) | none — last closed 06:31:00 | yes, `c59aeb96` |
| 08:52:46 (this campaign, full) | `5a90bd14` 08:45:00→08:54:00 | **no** |
| 09:35:36 (this campaign, no_memory) | `4cbf6a4f` 09:27:00→09:37:00 | **no** |

In both failing trials the preceding scenario's checkout-service incidents were
still open, 7–9 minutes old and well inside the 120-minute fold window. In both
succeeding trials they had closed 12–14 minutes earlier.

The harness's own failure message asserted a third explanation — dedup against
an already-open incident with the same alertname — which is also false here: the
only two incidents titled `[checkout-service] CheckoutMemoryApproachingLimit`
closed at 06:23:59 and 07:07:57, hours before either failing trial. Three
explanations were offered for this failure and all three were reached without
evidence. That is what #71 fixes: `_fire_alert` now keeps the webhook's own
receipt (`incidents_created` / `incidents_folded`), and `_diagnose_missing_incident`
names the absorbing incident instead of guessing.

The failure is **arm-symmetric** — it costs a pair but does not bias the
comparison. `BENCH_INCIDENT_WAIT_SECONDS` was deliberately *not* raised for the
second arm alone: that would have made the arms unequal in harness
configuration, and — now that the cause is known — would have bought no pair
anyway, since no wait of any length produces an incident that was folded away.

**Coverage caveat:** `checkout_memory_leak_oom`'s recorded signature match is
`…-oom-pdf-thumbnailer`, a different service from the scenario's
`checkout-service`. The match is on failure class, not service, so "COVERED" is
weaker for that scenario than the label suggests. Moot here — the scenario was
invalid — but it should not be read as a service-level hit.

**`payment_subthreshold_charge_errors` returns VERIFIED_RECOVERED at 7s** with no
root-cause or remediation grade. This is the unarmed negative control exiting via
the terminal-application-status path (the #65 recovery guard correctly did *not*
fire: `failure_observed: false`). A clean scenario has no meaningful MTTR, yet it
is folded into the MTTR aggregate.

> **Resolved, 2026-09-25 (#69).** The oracle vocabulary gained
> `NO_ACTION_CORRECT`. A scenario the corpus marks `taxonomy.category == "clean"`
> whose signal never leaves its healthy band now reports that verdict and no
> MTTR. It still counts as a resolved trial and still receives a full structured
> grade, because investigating and correctly taking no action is a pass — routing
> it through the unresolved branch would have recorded no diagnosis, remediation
> or safety outcome at all and graded the agent as having failed a scenario it
> passed. A control whose signal *did* go failing now reports INVALID_SCENARIO
> instead: the sub-threshold premise did not hold, so "take no action" no longer
> describes the correct handling of that run. See the correction in §3 for the
> effect on the numbers above.

## 6. Fixes verified in production during this campaign

- **#66** — teardown closing incidents that would otherwise absorb the next
  scenario's alert fired 4 times, e.g.
  `teardown: closed incident 85df7e8b (was pending_acknowledgment)`.
- **#68** — both arms verified their declared fingerprint against the first
  trial's live manifest.
- **#65** — the recovery-break guard correctly did not fire on the unarmed probe.

## 7. Artifacts

All under `reports/ablation-20260925/`:

```
trials.jsonl                 8 rows, sha256 20ec306fb0ae1d21426bd8ef664664d839d041a0a7c604923093d86927319ba6
ablation-default.json        the verdict above (schema_version 2)
coverage-holdout.json        org 9240f8b0…, cluster bcbd9577…, 4/4 observable, 0 incident points
full.meta / no_memory.meta   per-arm image, fingerprint, fingerprint source, harness commit
full.log / no_memory.log     full run logs
grades-<arm>.jsonl           per-trial structured grades
oracle-<arm>.jsonl           per-trial oracle verdicts
manifest-<arm>.json          first trial's manifest per arm
manifests-<arm>-all.json     every captured manifest per arm
```

Coverage was measured with the repo interpreter
(`/workspaces/Sre-automation/.venv/bin/python`, 3.12.3) via the semantic
retrieval path. The agent's own interpreter is `/app/.venv/bin/python`; these
two disagreed 6/6 against 3/6 before `_SEMANTIC_MATCH_FLOOR` was calibrated and
agree with the floor in place, but that agreement is an assumption of this
coverage figure rather than something re-verified here.

## 8. Restoration

The platform was returned to production configuration after the run: `.env`
restored to baseline sha256
`a7457739faae050618f0d11b626681b1a0c11379ba3c420e196e89ec777d2837`, both
`sre-agent-api` and `sre-temporal-worker` recreated, `experiment_active: False`
confirmed, and open incidents cleared through the sanctioned mark-resolved API.

## 9. What would make this conclusive

Not authorized, recorded for costing only: 20 paired trials per arm on the
holdout split, with incident memory actually populated for the tenant so the
recall half of the component is under test rather than inert, and the
same-service fold kept off the scenario boundary so trials are not silently
absorbed (see the correction in section 5; #71 now makes that visible when it
happens). Until then the honest statement is
the one above — no effect observed, evidence too thin to conclude there is none.
