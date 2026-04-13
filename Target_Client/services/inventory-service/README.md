# Inventory Service

This service simulates inventory lookup behavior and operational noise around stock, latency, and reindexing work.

The inventory path is the quieter sibling of the checkout path, but it is still intentionally noisy enough to generate useful evidence for the platform.

## Endpoints

- `GET /health` returns service health.
- `GET /metrics` exposes Prometheus metrics.
- `GET /items` lists all inventory items and emits stock warnings.
- `GET /items/{item_id}` fetches a single item or returns 404 when the item does not exist.
- `POST /reindex` performs an expensive CPU-bound operation to create resource pressure.
- `GET /admin/config` and `POST /admin/config` expose runtime tuning for query latency.

## Configuration

- `SLOW_QUERY_RATE` controls how often the simulated database query is slow.

## Operational Role

This service gives the platform a second source of latency and error conditions that are different from the checkout path. It is useful for distinguishing app-specific issues from shared infrastructure issues.

It also gives the platform signals that look like:

- slow reads,
- low-stock warnings,
- not-found responses,
- and CPU-heavy maintenance activity.

Those patterns make it easier for the supervisor to compare evidence sources and decide whether the issue is localized or systemic.

## What To Watch

- Low-stock warnings in logs.
- Slow query duration.
- 404 behavior for missing items.
- Reindex CPU spikes.

## Related Docs

- [../README.md](../README.md)
- [../../k8s/README.md](../../k8s/README.md)