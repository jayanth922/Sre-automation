# Target Client — Architecture

## Overview

A simulated e-commerce backend with deliberate fault injection, paired with a full observability stack. Designed as a sandbox for practicing SRE (Site Reliability Engineering) concepts.

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        TRAFFIC LAYER                            │
│                                                                 │
│                    ┌──────────────────┐                          │
│                    │  Load Generator  │                          │
│                    │  (fake customers)│                          │
│                    └────────┬─────────┘                          │
│                             │ HTTP requests                     │
│                             ▼                                   │
├─────────────────────────────────────────────────────────────────┤
│                      APPLICATION LAYER                          │
│                                                                 │
│                    ┌──────────────────────────────┐              │
│                    │        API Gateway           │              │
│                    │           :8000              │              │
│                    │                              │              │
│                    │   /docs ──► Swagger UI       │              │
│                    │   /health                    │              │
│                    │   /checkout/{id}             │              │
│                    │   /inventory                 │              │
│                    │   /metrics                   │              │
│                    └───┬──────────────────┬───────┘              │
│                        │                  │                      │
│                        ▼                  ▼                      │
│          ┌─────────────────┐    ┌─────────────────┐             │
│          │    Checkout     │    │   Inventory     │             │
│          │    Service      │    │   Service       │             │
│          │    :8001        │    │   :8002         │             │
│          │                 │    │                 │             │
│          │ ⚠ 15% errors   │    │ ⚠ 25% slow     │             │
│          │ ⚠ 20% slow     │    │   queries       │             │
│          └────────┬────────┘    └────────┬────────┘             │
│                   │                      │                      │
├───────────────────┼──────────────────────┼──────────────────────┤
│                   │  OBSERVABILITY LAYER │                      │
│                   │                      │                      │
│                   │    /metrics          │   /metrics           │
│         ┌─────────▼──────────────────────▼─────────┐            │
│         │              Prometheus                   │            │
│         │              :9090                        │            │
│         │         (scrapes metrics)                 │            │
│         └──────────────────┬───────────────────────┘            │
│                            │                                    │
│                            │ fires alerts                       │
│                            ▼                                    │
│         ┌──────────────────────────────────────────┐            │
│         │           Alertmanager                    │            │
│         │              :9093                        │            │
│         └──────────────────────────────────────────┘            │
│                                                                 │
│                                                                 │
│         ┌──────────────────┐      ┌────────────────┐            │
│         │    Promtail      │─────▶│     Loki       │            │
│         │ (reads container │      │    :3100       │            │
│         │     logs)        │      │ (stores logs)  │            │
│         └──────────────────┘      └───────┬────────┘            │
│                                           │                     │
├───────────────────────────────────────────┼─────────────────────┤
│                   VISUALIZATION LAYER     │                     │
│                                           │                     │
│         ┌─────────────────────────────────▼─────────┐           │
│         │              Grafana                       │           │
│         │              :3000                         │           │
│         │                                            │           │
│         │   ◀── queries Prometheus (metrics)         │           │
│         │   ◀── queries Loki (logs)                  │           │
│         └────────────────────────────────────────────┘           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### Application Layer

| Service | Port | Description |
|---------|------|-------------|
| **API Gateway** | 8000 | Entry point for all requests. Routes to Checkout and Inventory services. Exposes Swagger UI at `/docs`. |
| **Checkout Service** | 8001 | Handles order checkouts. Deliberately fails ~15% of requests and is slow ~20% of the time. Has a "chaos mode" that spikes errors to ~50%. |
| **Inventory Service** | 8002 | Serves product inventory data (5 items). Deliberately has slow queries ~25% of the time. |
| **Load Generator** | — | Python script that sends ~5 req/s steady traffic with periodic bursts of ~20 req/s every 60 seconds. |

### Observability Layer

| Tool | Port | Description |
|------|------|-------------|
| **Prometheus** | 9090 | Scrapes `/metrics` endpoints from all services. Evaluates alert rules. |
| **Alertmanager** | 9093 | Receives alerts from Prometheus. Routes and groups alerts. Sends webhook notifications. |
| **Loki** | 3100 | Log aggregation system. Stores and indexes logs pushed by Promtail. |
| **Promtail** | — | Agent that reads Docker container logs and pushes them to Loki. |
| **Grafana** | 3000 | Visualization UI. Queries Prometheus for metrics and Loki for logs. |

## Data Flow Summary

| From | To | Direction | What Flows |
|------|----|-----------|------------|
| Load Generator | API Gateway | → | HTTP requests (fake customer traffic) |
| API Gateway | Checkout / Inventory | → | Routed requests |
| Prometheus | Services | ← (scrapes) | Pulls metrics from `/metrics` endpoints |
| Prometheus | Alertmanager | → | Fires alert notifications |
| Promtail | Loki | → | Pushes collected container logs |
| Grafana | Prometheus | ← (queries) | Pulls metrics for dashboards |
| Grafana | Loki | ← (queries) | Pulls logs for exploration |

## Fault Injection Configuration

Controlled via `.env` file:

| Variable | Default | Effect |
|----------|---------|--------|
| `CHECKOUT_ERROR_RATE` | 0.15 | 15% of checkout requests return errors |
| `CHECKOUT_SLOW_RATE` | 0.20 | 20% of checkout requests are slow |
| `INVENTORY_SLOW_QUERY_RATE` | 0.25 | 25% of inventory queries are slow |
| `CHAOS_MODE` | false | Set to `true` to spike checkout errors to ~50% |
| `LOAD_RPS` | 5 | Steady-state requests per second |
| `LOAD_BURST_RPS` | 20 | Burst requests per second |
| `LOAD_BURST_DURATION` | 15 | Burst duration in seconds |
| `LOAD_BURST_INTERVAL` | 60 | Seconds between bursts |
