# High Latency — Elevated Response Times

**Alert Name:** CheckoutHighLatency, InventorySlowQueries, PaymentServiceHighLatency
**Service:** checkout-service / payment-service / inventory-service
**Incident Type:** latency
**Severity:** SEV2
**Owner Team:** meridian-oncall

## Summary

Slow is not broken, and slow here is usually somebody else's fault. The
service that fires the alert is frequently not the service to act on. Work the
decision procedure in order and stop at the first branch that matches.

Act only at or above these thresholds. Below them the service is healthy.

| Signal | Query metric | Act at or above |
|---|---|---|
| checkout-service p95 | `http_request_duration_seconds_bucket` | **1.5 s** |
| payment-service p95 | `http_request_duration_seconds_bucket` | **1.0 s** |
| inventory-service db p90 | `db_query_duration_seconds_bucket` | **1.0 s** |

## Decision procedure

**Step 1 — Is the alerting service actually above threshold?**

```
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="<service>"}[5m])))
```

For InventorySlowQueries use the database histogram instead:

```
histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))
```

If the measured value is **below** the threshold in the table → **Branch A**.
Stop here. A few slow calls do not move a 90th or 95th percentile; if the
percentile has not moved, the objective is not being missed.

**Step 2 — Confirm this is latency and not failure.**

```
sum(rate(http_errors_total{service="<service>"}[5m])) / clamp_min(sum(rate(http_requests_total{service="<service>"}[5m])), 1)
```

If the error ratio is *also* above its threshold (checkout 0.45, payment
0.10), stop using this runbook and work **High Error Rate** instead.

**Step 3 — For CheckoutHighLatency only: is payment-service the source?**

```
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="payment-service"}[5m])))
sum(rate(http_errors_total{service="payment-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="payment-service"}[5m])), 1)
```

- payment **p95 ≥ 1.0 s** and rising ahead of checkout → **Branch C**
- payment **error ratio ≥ 0.10** → **Branch D**
- payment healthy on both → **Branch B**

**Step 4 — For InventorySlowQueries** with db p90 above threshold →
**Branch E**.

## Branch A — Action: none, close the incident

The percentile is inside its healthy band — checkout p95 below **1.5 s**,
payment p95 below **1.0 s**, inventory db p90 below **1.0 s**.

- **Do:** record the measured percentile and the threshold it was compared
  against, then close as **no action required**.
- **Do NOT** restart, roll back, revert, scale, patch or change config.

Perturbing a few calls is not an incident. Acting here disrupts a healthy
service and is scored as a failure.

## Branch B — Action: restart or scale (checkout's own latency)

Checkout is slow on its own account; its dependencies are healthy and its
error ratio is unchanged.

1. `restart_deployment(name="checkout-service", namespace="meridian")` — clears
   accumulated contention.
2. If latency returns under load, `scale_deployment(name="checkout-service", replicas=<current+2>, namespace="meridian")`.
3. If a recent deployment correlates with onset,
   `rollback_deployment(name="checkout-service", namespace="meridian")`.

All three are permitted here. Scale is permitted **only** in this branch.

## Branch C — Action: restart payment, escalate, scale checkout (slow dependency)

payment-service charge latency is elevated and leads checkout's. Checkout is
waiting, not failing.

1. `restart_deployment(name="payment-service", namespace="meridian")` — act on
   the slow service, not the one that alerted.
2. `scale_deployment(name="checkout-service", replicas=<current+2>, namespace="meridian")`
   — permitted here: extra concurrency absorbs the wait while payment recovers.
3. Escalate to the payment-service owner.

- **Do NOT roll back and do NOT revert any commit on checkout-service.**
  Checkout's code is fine; the latency is imported from payment. A rollback
  removes a good release and changes nothing.

## Branch D — Action: restart payment and escalate (erroring dependency)

payment-service error ratio is above 0.10. Checkout's p95 is inflated by
inline one-second retry sleeps against payment `/charge` — the latency is
retry cost, and it disappears when payment stops erroring.

1. `restart_deployment(name="payment-service", namespace="meridian")`
2. Escalate to the payment-service owner.

- **Do NOT scale checkout-service.** Unlike Branch C, the dependency is
  *failing*, not merely slow. More checkout replicas means proportionally more
  retries against a failing dependency, which makes it worse.
- **Do NOT roll back and do NOT revert any commit on checkout-service.**
  Checkout has no deploy or config change correlated with the onset.

## Branch E — Action: restart, patch or change config (slow queries)

inventory-service database p90 is above 1.0 s. This is a query, index, lock or
pool problem inside inventory-service.

1. `get_deployment_config(name="inventory-service", namespace="meridian")`
2. `patch_deployment_env(name="inventory-service", namespace="meridian", env={...})`
   — apply the query, pool or index setting the evidence points to.
3. `restart_deployment(name="inventory-service", namespace="meridian")` to
   apply it and clear held locks.

- **Do NOT scale inventory-service.** Slow queries are a per-query cost.
  Additional replicas issue the same slow queries against the same database
  and add contention.

## Verification

Re-run the branch's probe. Recovery is credited only after **2 consecutive
passes**.

| Branch | Probe query | Passes when |
|---|---|---|
| B (checkout latency) | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="checkout-service"}[5m])))` | `< 1.5` |
| C (payment slow) | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="payment-service"}[5m])))` | `< 1.0` |
| D (payment erroring) | `sum(rate(http_errors_total{service="payment-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="payment-service"}[5m])), 1)` | `< 0.10` |
| E (inventory queries) | `histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))` | `< 1.0` |

In Branches C and D the probe is on **payment-service**, not on the service
that alerted. Checkout latency recovers as a consequence; do not verify
against checkout.

## Background

Facts about Meridian that change the diagnosis:

- **checkout-service and inventory-service share no call path.**
  checkout holds inventory locally. Inventory query latency moving during a
  checkout incident — or checkout latency moving during an inventory incident
  — is a coincidence and is never evidence. Both directions of this decoy
  appear in practice.
- **checkout retries payment inline with a one-second sleep.** This is why a
  payment *error* problem presents as a checkout *latency* alert.
- A percentile is not a mean. A small fraction of slow calls leaves p90 and
  p95 untouched; that is the intended behaviour, not a blind spot.
