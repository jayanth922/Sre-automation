# Downstream Dependency Failure — Cascading Errors

**Alert Name:** PaymentProviderDown, DownstreamDependencyFailure
**Service:** payment-service → checkout-service
**Incident Type:** dependency
**Severity:** SEV1
**Owner Team:** meridian-oncall

## Summary

The external payment provider is unreachable. Meridian code is healthy; the
services that alert are reporting someone else's outage. Recovery is gated on
the provider coming back, so the job here is to stop the damage spreading,
hand off to the party who can actually fix it, and verify against the
dependency rather than against the symptom.

## Decision procedure

**Step 1 — Confirm the dependency is actually down.**

```
min(payment_provider_up{service="payment-service"})
```

- Returns **0** → the provider is down. Continue to Step 2.
- Returns **1** → this is not a dependency outage. Work **High Error Rate**
  instead; the fault is inside a Meridian service.

**Step 2 — Confirm Meridian itself has not regressed.**

```
sum(rate(http_errors_total{service="checkout-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="checkout-service"}[5m])), 1)
```

Checkout errors during a provider outage carry the reason
`payment_dependency_failure`:

```
query_logs(logql='{app="checkout-service"} |= "payment_dependency_failure"', limit=5, start_time="<alert-start-minus-5m>", end_time="<alert-start-plus-5m>")
```

If the reasons are dependency-side and no deployment correlates with the
onset, checkout has no regression of its own. Proceed to remediation and do
not act on checkout.

## Action

1. `restart_deployment(name="payment-service", namespace="meridian")` — clears
   connections and retry state stuck against the dead provider, so
   payment-service recovers the moment the provider returns instead of
   staying wedged.
2. **Escalate to the payment provider's on-call.** This is the step that
   resolves the incident. Recovery is not in Meridian's control, so escalate
   early rather than after exhausting local actions.
3. If a payment-service release correlates with the onset — for example a
   change to provider endpoints, credentials or timeouts —
   `rollback_deployment(name="payment-service", namespace="meridian")`.
   Only when a release actually correlates; otherwise skip this step.

- **Do NOT scale any service.** The dependency is down, not saturated. More
  replicas open more connections to an endpoint that is refusing all of them,
  add retry load, and change nothing about availability.
- **Every step above targets payment-service. checkout-service is out of
  scope for this incident.** It is correctly reporting an upstream failure and
  has no fault of its own, so any remediation aimed at it disrupts a healthy
  service while leaving the outage untouched. Its elevated error ratio is a
  symptom to verify against later, never a target to act on.

## Verification

```
min(payment_provider_up{service="payment-service"})
```

Passes when **>= 1**, confirmed on **2 consecutive passes**.

Verify against the **dependency**, not against the checkout error ratio.
Checkout recovers as a consequence, and it will still be carrying its normal
0.35 baseline error ratio afterwards — that baseline is inventory holds and is
not a residual failure.

## Background

- **`payment_provider_up` is the authoritative signal.** It distinguishes a
  provider outage from a payment-service fault, and those two have opposite
  remediations. Check it before anything else on any payment-related alert.
- **checkout-service surfaces provider failures as its own 5xx.** This is why
  a provider outage can page as `CheckoutHighErrorRate` rather than
  `PaymentProviderDown`. The High Error Rate runbook routes that case back
  here at its Step 3.
- checkout-service carries a standing **0.35** error ratio from inventory
  holds. Do not read it as evidence of a checkout regression, and do not wait
  for zero.
- The default namespace on the executor tools is `demo-app`, which does not
  exist here. Always pass `namespace="meridian"` explicitly.
