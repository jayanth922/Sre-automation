# v2 incident-type coverage

What kinds of software incident this corpus can and cannot put the agent under,
and why. Written for task #4 ("test varied software incident types") after an
offline audit of all 22 scenarios — no trials were run to produce it.

## What the corpus holds

22 scenarios, 11 taxonomy categories, 3 splits:

| split   | n  | categories covered | missing |
|---------|----|--------------------|---------|
| train   | 12 | ambiguous, application, clean, dependency, deployment, multi_fault, noisy | capacity, configuration, infrastructure, resource |
| dev     | 6  | capacity, configuration, dependency, infrastructure, multi_fault, noisy | ambiguous, application, clean, deployment, resource |
| holdout | 4  | clean, dependency, noisy, resource | the other 7 |

## The injection surface is the ceiling

Every one of the 22 scenarios uses the same adapter, `meridian_admin_config_v1`,
and every fault is a POST to `/admin/config` on a Meridian service. Across all
25 fault contracts the corpus sets exactly seven knobs:

| knob | contracts |
|------|-----------|
| `error_rate` | 9 |
| `slow_rate` | 7 |
| `slow_query_rate` | 5 |
| `leak_kb_per_request` | 3 |
| `provider_down` | 2 |
| `chaos_mode` | 1 |
| `rps` | 1 |

and touches four targets: checkout-service (10), payment-service (9),
inventory-service (5), load-generator (1).

**Those seven knobs are the entire vocabulary the harness knows.** A repo-wide
search for other `/admin/config` payload keys finds none — the corpus already
exercises 100% of the injection surface available to it. So the limit on
incident-type breadth is the Meridian application, not the dataset: adding a
scenario for an incident class the app cannot simulate is writing a scenario
that cannot be run.

## What that excludes

Incident classes with no representation, because no knob produces them:

- queue / consumer-lag backlog
- connection- or thread-pool exhaustion as a fault in its own right (only
  reachable indirectly, via `chaos_mode` on checkout)
- cache stampede or cold cache
- rate-limit / quota exhaustion (429 from a dependency)
- certificate or TLS expiry
- disk-full and log-volume exhaustion
- clock skew
- failed schema migration
- partial or canary rollout failure
- poison message / data corruption
- authn/authz failure loops
- DNS resolution failure

Every one of these is an ordinary production incident type. None is testable
here today.

## One scenario asks for evidence that cannot exist

`train/bad_deploy_checkout` is the corpus's only `deployment` scenario. Its
root-cause keywords are `deploy, commit, rollback, regression, error rate,
release` and it expects "deployment or release evidence identifies the
checkout-service change" — but its injected fault is `error_rate: 0.5`, the
same knob four other scenarios use for plain application errors. Nothing
deploys, so there is no release artefact to find and no commit to revert. The
agent is asked to identify a change that was never made, and the only evidence
it can reach says "application error rate".

This is the positive case. Three further scenarios mention deploys
(`dev/checkout_db_connection_errors`, `train/payment_dependency_cascade`,
`train/payment_errors_cascade_to_checkout_latency`), but each makes a *negative*
claim — "no deploy or config change correlated with the onset" — which the
absence of any deploy satisfies correctly. Those three are sound as written.

`revert_commit` appears in `allowed_action_types` for 7 scenarios and
`forbidden_action_types` for 8. The forbidden half is meaningful: it tests that
the agent does not reach for a rollback when nothing was released. The allowed
half is not, because the action has no referent.

## What this means for results already published

The two paid campaigns (#28, #70) both ran on **holdout**, which is 4 scenarios
covering 4 of 11 categories. It contains no configuration, infrastructure,
application, multi_fault, deployment, capacity or ambiguous case. Any claim from
those campaigns is a claim about dependency outage, memory leak, a noisy
concurrent signal, and a negative control — not about incident types in general.

This compounds a point already recorded in `docs/ai/PROJECT_STATE.md`: the
20-pair campaign costed at ≈$47 means five repetitions of these same four
scenarios, so it buys run-to-run variance, not breadth. Breadth is not
purchasable at any price until the app can simulate more.

## What would move this

In dependency order:

1. **Meridian knobs.** Each new incident class needs a knob on `/admin/config`
   and a Prometheus signal that moves with it, or the recovery oracle has
   nothing to probe. Cheapest additions with existing metrics: a queue-depth
   gauge, a pool-exhaustion mode distinct from `chaos_mode`, a 429 mode on the
   payment provider.
2. **A deploy adapter.** Until one exists, `bad_deploy_checkout` should either
   be marked unrunnable or have its expected evidence rewritten to what the
   error-rate injection actually produces. It currently costs a paid trial to
   record a failure the corpus guarantees in advance.
3. **Rebalance holdout.** 4 scenarios is too few to separate arms and too narrow
   to generalise. Growth here is free — it is dataset work, not app work — but
   only after (1), or the new scenarios draw from the same seven knobs.

Steps 2 and 3 cost nothing to do. Step 1 is the real work, and it is
application work outside this repo.
