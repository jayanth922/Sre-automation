# Prometheus MCP Server

This service exposes metric queries over MCP using the customer-target Prometheus endpoint.

## Responsibilities

- Connect to Prometheus through the `PROMETHEUS_URL` environment variable.
- Expose metric and time-series query tools to the platform.
- Return structured numeric evidence for the metrics-specialist agent.

## Why It Exists

The agent uses this server when it needs a live view of golden signals, alert conditions, or service health metrics from the target environment.

This is the numeric evidence source for the investigation loop. It is where latency, traffic, error, and saturation questions get answered.

## Configuration

- `PROMETHEUS_URL` defaults to `http://host.docker.internal:9090`.
- `HOST` defaults to `0.0.0.0`.
- The compose file publishes the service on host port `4001`.

## Operational Notes

- The agent uses this server when it needs query-driven metric evidence rather than a static summary.
- The response shape should stay structured so the supervisor can compare signal trends without re-parsing raw PromQL results in the UI layer.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)