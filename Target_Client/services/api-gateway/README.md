# API Gateway

This service is the entry point for the target-client traffic path. It receives user requests, forwards them to the checkout or inventory services, and publishes request metrics for the observability stack.

The gateway is the most visible piece of the customer simulation because it is the front door for both the normal traffic path and the load generator traffic.

## Endpoints

- `GET /health` returns a service health check.
- `GET /metrics` exposes Prometheus metrics.
- `POST /checkout/{order_id}` forwards an order to the checkout service.
- `GET /inventory` fetches the current inventory list.
- `GET /inventory/{item_id}` fetches a single inventory item.

## Configuration

- `CHECKOUT_SERVICE_URL` defaults to `http://checkout-service:8001`.
- `INVENTORY_SERVICE_URL` defaults to `http://inventory-service:8002`.

## Operational Role

The gateway is the easiest place to observe request volume, latency, and upstream failures. The load generator sends most of its traffic through this service, which makes it a useful anchor for smoke tests and incident correlation.

The gateway is also a good sanity check for the rest of the target client. If this service is healthy but downstream traffic is failing, the problem is probably in one of the services it calls rather than in the request entry point itself.

## What To Watch

- Request rate spikes.
- Upstream 5xx responses.
- Timeout behavior when downstream services are slow.
- Prometheus scrape visibility.

## Related Docs

- [../README.md](../README.md)
- [../../k8s/README.md](../../k8s/README.md)