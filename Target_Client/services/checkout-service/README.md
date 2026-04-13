# Checkout Service

This service simulates order processing and payment behavior. It is intentionally flaky so the platform can observe failures, slow requests, and changing resource usage.

The checkout path is deliberately more fragile than the inventory path because it is the easiest way to create an incident that looks operationally realistic.

## Endpoints

- `GET /health` returns service health and chaos-mode state.
- `GET /metrics` exposes Prometheus metrics.
- `POST /process` processes an order and may fail or slow down depending on the configured rates.
- `GET /admin/config` and `POST /admin/config` expose runtime tuning for error, slow, and chaos settings.

## Configuration

- `ERROR_RATE` controls the payment-failure probability.
- `SLOW_RATE` controls the chance of slow processing.
- `CHAOS_MODE` increases the probability of database-style errors.

## Operational Role

The checkout service is the primary source of payment failures and latency spikes in the demo. It also simulates a tiny memory leak so the observability stack has resource signals to track over time.

The service is useful when you want the platform to see a mix of:

- successful requests,
- hard failures,
- slow responses,
- and resource drift.

That combination is what makes the incident flow interesting to investigate.

## What To Watch

- Payment failure counters.
- Slow request latency.
- Simulated memory growth.
- Chaos mode toggles.

## Related Docs

- [../README.md](../README.md)
- [../../k8s/README.md](../../k8s/README.md)