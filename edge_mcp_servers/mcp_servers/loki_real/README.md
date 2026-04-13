# Loki MCP Server

This service exposes log-search capability over MCP using the customer-target Loki endpoint.

## Responsibilities

- Connect to Loki through the `LOKI_URL` environment variable.
- Expose log-search and log-context tools to the platform.
- Return structured results that the agent can turn into incident evidence.

## Why It Exists

The platform uses this server to inspect application logs from the target environment without giving the reasoning engine direct knowledge of the Loki client library or the container network topology.

That lets the logs specialist focus on evidence extraction while the runtime stays responsible for interpretation.

## Configuration

- `LOKI_URL` defaults to `http://host.docker.internal:3100`.
- `HOST` defaults to `0.0.0.0`.
- The compose file publishes the service on host port `4002`.

## Operational Notes

- This server is the main way the agent gets application logs from the target environment.
- The service should return structured log context that can be summarized by the supervisor and attached to incident transcripts.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)