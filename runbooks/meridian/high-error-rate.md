# High Error Rate — Elevated 5xx / Application Errors

**Alert Name:** CheckoutHighErrorRate, PaymentServiceHighErrorRate, PaymentFailureSpike, InventoryHighErrorRate
**Service:** checkout-service / payment-service / inventory-service
**Incident Type:** error_rate
**Severity:** SEV1
**Owner Team:** meridian-oncall

## Summary

Four different faults raise these alerts and they need four different fixes.
Work the decision procedure below in order and stop at the first branch that
matches. Do not skip to remediation: the wrong action on the right alert is
scored as a failure, and three of the four branches forbid the action that
looks most obvious.

Act only at or above these thresholds. Below them the service is healthy.

| Service | Signal | Healthy baseline | Act at or above |
|---|---|---|---|
| checkout-service | error ratio | 0.35 (inventory holds) | **0.45** |
| payment-service | error ratio | ~0.00 | **0.10** |
| inventory-service | error rate | 3.3% ratio | **0.3 errors/sec** |

## Decision procedure

Run these in order. Each step is a query and a test; the first test that
passes selects the branch.

**Step 1 — Is it actually above threshold?**

```
sum(rate(http_errors_total{service="<service>"}[5m])) / clamp_min(sum(rate(http_requests_total{service="<service>"}[5m])), 1)
```

Compare to the table above. If the measured value is **below** the "act at or
above" threshold → **Branch A**. Stop here.

**Step 2 — Is the error *count* up while the error *ratio* is flat?**

```
sum(rate(http_requests_total{service="<service>"}[5m]))
```

If request volume has risen and the ratio from Step 1 is still at its healthy
baseline, the service is failing the same fraction of a larger load. That is a
traffic shift, not a fault → **Branch B**.

**Step 3 — Is the external payment provider down?**

```
min(payment_provider_up{service="payment-service"})
```

If this returns **0** → **Branch C**. This holds whether the alert fired on
payment-service or on checkout-service: checkout surfaces provider failures as
its own 5xx.

**Step 4 — Are the errors database-side?**

```
query_logs(logql='{app="<service>"} |= "db_pool_exhausted"', limit=5, start_time="<alert-start-minus-5m>", end_time="<alert-start-plus-5m>")
query_logs(logql='{app="<service>"} |= "db_connection_refused"', limit=5, start_time="<alert-start-minus-5m>", end_time="<alert-start-plus-5m>")
query_logs(logql='{app="<service>"} |= "db_timeout"', limit=5, start_time="<alert-start-minus-5m>", end_time="<alert-start-plus-5m>")
```

If any of the three returns lines → **Branch D**.

**Step 5 — Otherwise the fault is in this service's own code or config** →
**Branch E**.

## Branch A — Action: none, close the incident

The measured value is below the acting threshold — checkout below **0.45**,
payment below **0.10**, inventory below **0.3 errors/sec**. The service is
inside its healthy band and no objective is being missed.

- **Do:** record the measured value and the threshold it was compared against,
  then close the incident as **no action required**.
- **Do NOT** restart, roll back, revert, scale, patch or change config. There
  is no fault to fix, and every one of these actions causes a real disruption
  to a healthy service.

A sub-threshold alert is a working alert, not a missed incident.

## Branch B — Action: escalate only (traffic shift)

Error volume tracks request volume; the failure *fraction* is unchanged. The
errors are client-driven — typically lookups for item ids that do not exist —
and the service is behaving correctly.

- **Do:** escalate to the service owner with the request-rate and
  error-ratio series attached, so the traffic source can be identified.
- **Do NOT** restart, roll back, revert, scale, patch or change config. The
  service is not broken; scaling it would serve more of the same client errors.

## Branch C — Action: restart, then escalate (provider outage)

`payment_provider_up` is 0. The failure is outside Meridian entirely. Checkout
errors here carry the reason `payment_dependency_failure`.

1. `restart_deployment(name="payment-service", namespace="meridian")` — clears
   connections stuck against the dead provider.
2. Escalate to the payment provider's on-call. Recovery depends on them.

- **Do NOT roll back checkout-service and do NOT revert any checkout commit.**
  No checkout code changed. Rolling back an unrelated release removes a good
  version while the outage continues, and the error ratio will not move.
- **Do NOT scale.** More replicas mean more connections to a provider that is
  down.

If the alert was `PaymentProviderDown` rather than a checkout alert, use the
**Downstream Dependency Failure** runbook, which covers the payment-service
release case this branch deliberately excludes.

## Branch D — Action: restart and config change (database connectivity)

Error reasons are `db_connection_refused`, `db_timeout` or
`db_pool_exhausted`. These are database-side, not the gateway reasons a bad
release produces.

1. `get_deployment_config(name="<service>", namespace="meridian")` — read the
   current pool settings.
2. If the pool is exhausted, raise it:
   `patch_deployment_env(name="<service>", namespace="meridian", env={"DB_POOL_SIZE": "<larger value>"})`
3. `restart_deployment(name="<service>", namespace="meridian")` — drops
   half-open connections and applies the new pool.
4. If errors persist after verification, escalate to the database owner.

- **Do NOT revert a commit.** No commit introduced this; there is nothing to
  revert and the revert will not clear the connection errors.
- **Do NOT scale.** Each new replica opens its own pool against the same
  database and makes exhaustion worse.

## Branch E — Action: roll back the change (service-side regression)

The fault is in this service. Reasons are gateway- or charge-side
(`payment_gateway_timeout`, `card_declined`, `fraud_detected`,
`gateway_unavailable`, `insufficient_funds`, `provider_timeout`), the provider
is up, and the database is healthy.

1. Identify the change correlated with onset — the most recent deployment or
   release on the erroring service.
2. `rollback_deployment(name="<service>", namespace="meridian")` — the primary
   action. Prefer this over everything else.
3. If no deployment correlates with the onset, `restart_deployment(name="<service>", namespace="meridian")`.
4. If neither recovers within two verification passes, escalate.

- **Do NOT scale.** An elevated error *ratio* is not a capacity problem. Adding
  replicas multiplies the failing code path and leaves the ratio unchanged.

## Verification

Re-run the branch's probe below. Recovery is credited only after **2
consecutive passes**. Do not declare success on a single reading.

| Alert / service | Probe query | Passes when |
|---|---|---|
| CheckoutHighErrorRate | `sum(rate(http_errors_total{service="checkout-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="checkout-service"}[5m])), 1)` | `< 0.45` |
| PaymentServiceHighErrorRate | `sum(rate(http_errors_total{service="payment-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="payment-service"}[5m])), 1)` | `< 0.10` |
| PaymentFailureSpike | `sum(rate(payment_failures_total{job="checkout-service"}[5m]))` | `< 0.4` |
| InventoryHighErrorRate | `sum(rate(http_errors_total{service="inventory-service"}[5m]))` | `< 0.3` |
| Branch C (provider) | `min(payment_provider_up{service="payment-service"})` | `>= 1` |

For checkout-service, returning to the **0.35** baseline is success. Waiting
for zero will never pass — the baseline is inventory-hold rejections, which
are normal.

## Background

Facts about Meridian that change the diagnosis, and are not discoverable from
the metrics alone:

- **checkout-service holds inventory locally and never calls
  inventory-service.** Perturbed inventory query latency during a checkout
  incident is a coincidence, never a cause. Do not chase it.
- **`payment_failures_total{job="checkout-service"}` is a checkout-side
  counter** despite the name. It rising does not imply payment-service is
  unhealthy; check payment-service's own error ratio before blaming it.
- **checkout-service carries a standing 0.35 error ratio.** Any comparison
  against zero will misread a healthy service as broken.
- checkout absorbs many payment failures with inline retries, so a real
  payment-service fault can show up as checkout *latency* rather than checkout
  errors. See the High Latency runbook.
