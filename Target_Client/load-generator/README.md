# Load Generator

This service continuously generates traffic against the API gateway so the demo has realistic load, failure bursts, and time-based signal changes.

## Behavior

- Mixes checkout and inventory requests.
- Triggers burst mode on a timer or through the admin API.
- Uses random item and order identifiers to vary the logs.
- Exposes a small admin server so you can adjust request rates at runtime.

The design is intentionally simple: it exists to keep the target environment interesting enough for the platform to observe.

## Ports And Endpoints

- Main load generator logic targets the API gateway through `GATEWAY_URL`.
- The admin API listens on port `8003`.
- `GET /admin/config` reads the current rate settings.
- `POST /admin/config` updates the rate settings.
- `POST /admin/trigger-burst` starts a manual burst.
- `GET /health` returns the generator health.

## Configuration

- `GATEWAY_URL` defaults to `http://api-gateway:8000`.
- `RPS` sets the steady-state request rate.
- `BURST_RPS`, `BURST_DURATION`, and `BURST_INTERVAL` control burst behavior.

## Operational Role

This service is the reason the target client keeps producing useful incidents after startup. Without it, the gateway and downstream services would be mostly idle and the observability signals would be too quiet for the platform story.

It also gives operators a way to move the system from “quiet” to “interesting” without editing code, which is useful when you are trying to reproduce a specific incident pattern.

## What To Watch

- RPS changes over time.
- Burst behavior and manual trigger support.
- Gateway response mix during high traffic.
- Correlation between load spikes and downstream error or latency spikes.

## Related Docs

- [../README.md](../README.md)
- [../services/README.md](../services/README.md)
- [../testing/README.md](../testing/README.md)