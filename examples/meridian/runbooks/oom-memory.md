# OOM — Container Killed for Memory Usage

**Alert Name:** CheckoutMemoryApproachingLimit, CheckoutServiceOOMKilled
**Service:** checkout-service
**Incident Type:** memory
**Severity:** SEV1
**Owner Team:** meridian-oncall

## Summary

A leaking process does not recover on its own and cannot be argued back down.
Only replacing the process frees the retained bytes. Everything else in this
runbook is about making sure the leak does not come straight back.

Act at or above **150 MB** (`150000000` bytes).

## Decision procedure

**Step 1 — Measure, and confirm the shape is a leak.**

```
max(process_memory_bytes_simulated{service="checkout-service"})
```

If below **150000000** → **Branch A**. Stop.

Otherwise read the series over the last 30 minutes. A leak **climbs
monotonically and never falls on its own**. If memory is sawtoothing — rising
and falling — this is normal allocation churn, not a leak; treat it as
Branch A.

**Step 2 — Is a deployment correlated with the onset of the climb?**

If yes → **Branch B**, both steps. If no → **Branch B**, step 1 only.

**Step 3 — Check whether any other signal is genuinely degraded.**

```
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="checkout-service"}[5m])))
sum(rate(http_errors_total{service="checkout-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="checkout-service"}[5m])), 1)
```

Elevated checkout latency alongside a leak is a **second, independent fault** —
the leak does not explain it. Report both; remediate the memory here and work
the latency separately under High Latency. Do not let a second signal talk you
out of the restart.

## Branch A — Action: none, close the incident

Memory is below 150 MB, or the series rises and falls rather than climbing.

- **Do:** record the measured value and close as **no action required**.
- **Do NOT** restart, roll back, revert, scale, patch or change config.

## Branch B — Action: replace the process, then remove the cause

**Step 1 — Replace the process. This is mandatory and it is what recovers the
incident.**

```
restart_deployment(name="checkout-service", namespace="meridian")
```

Retained bytes are only released when the process exits. No configuration
change, environment edit, limit patch or commit revert will bring the gauge
down on its own — the memory is already held.

**Step 2 — If a deployment correlates with the onset, remove the cause too.**

```
rollback_deployment(name="checkout-service", namespace="meridian")
```

or revert the identified commit. This prevents the leak from returning; it is
not what recovers this incident. Step 1 still has to happen.

- **Do NOT scale checkout-service.** Every new replica runs the same leaking
  code and starts its own climb. Scaling raises total memory consumption,
  spreads the fault across more pods, and delays the OOM kill rather than
  preventing it. This is the single most common wrong action on this alert.
- **Do NOT** raise the memory limit as the remediation. A larger ceiling
  changes only how long it takes to hit it.

## Verification

```
max(process_memory_bytes_simulated{service="checkout-service"})
```

Passes when **< 150000000**, confirmed on **2 consecutive passes**.

Expect a step change, not a decay: the gauge drops the moment the process is
replaced. If it has not dropped, the restart did not take effect — check that
the namespace was `meridian` and re-issue before trying anything else.

## Background

- **Retained bytes scale with `/process` call volume.** Under steady traffic
  the climb is close to linear, which makes the onset easy to date from the
  series and easy to correlate with a release.
- **checkout-service and inventory-service share no call path.** Inventory
  query latency perturbed during a checkout memory incident is a decoy. It
  cannot explain checkout memory and must not appear as supporting evidence.
- The default namespace on the executor tools is `demo-app`, which does not
  exist here. Always pass `namespace="meridian"` explicitly or the call
  silently targets nothing.
