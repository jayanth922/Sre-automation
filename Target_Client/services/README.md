# Target Services

This folder contains the three demo microservices that the API gateway fronts. They are intentionally simple, but they produce enough variability to drive the incident workflows in the platform.

The services are small on purpose. They are designed to be readable, deterministic in structure, and noisy in behavior.

## Services

- [api-gateway/](api-gateway/) receives client traffic and routes it to the downstream services.
- [checkout-service/](checkout-service/) simulates order processing and payment failures.
- [inventory-service/](inventory-service/) simulates inventory lookup, stock warnings, and slow database queries.

## Shared Patterns

All three services follow the same operational pattern:

- FastAPI as the HTTP framework.
- Prometheus metrics exposed at `/metrics`.
- Structured JSON logging.
- Small, explicit environment-variable configuration.

The services are built into container images and then injected into Kubernetes by [../start.sh](../start.sh).

## What Makes This Folder Important

The agent, dashboard, and edge stack all depend on these services producing realistic metrics and logs. When incidents appear in the platform, they usually originate from one of these behaviors:

- checkout failures,
- inventory slowdowns,
- gateway routing errors,
- or a combination of load and monitoring pressure.

The service layer gives the rest of the repository something concrete to observe and explain.

## Operational Expectations

- The gateway should be the first hop for most user-like traffic.
- The checkout service should occasionally fail or slow down.
- The inventory service should occasionally show query latency and missing items.
- The metrics emitted here should be sufficient for the observability stack to produce meaningful signals.

## Related Docs

- [../README.md](../README.md)
- [../k8s/README.md](../k8s/README.md)