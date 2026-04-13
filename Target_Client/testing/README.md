# Target Client Testing

This folder contains the operational smoke tests for the target-client environment. They are intentionally simple and verify that the demo stack is producing the right kinds of signals for the platform.

## Test Layers

- [test_layer0.py](test_layer0.py) validates the API gateway and downstream service behavior.
- [test_layer1.py](test_layer1.py) validates the observability stack and scrape targets.

## How To Run

Run the scripts directly with Python after the target client is up:

```bash
python test_layer0.py
python test_layer1.py
```

The tests expect the Kubernetes-based demo stack and the monitoring components to be available on their default localhost ports.

## What They Prove

- Layer 0 confirms the user-facing microservices are reachable.
- Layer 1 confirms Prometheus, Alertmanager, and Loki are healthy and that Prometheus can see the target services.

## Reading The Results

These tests are meant to tell you whether the target environment is viable for the platform demo, not whether it is healthy in the abstract. A failure in layer 0 usually means the gateway or downstream services are unavailable. A failure in layer 1 usually means the telemetry path is broken or incomplete.

## Related Docs

- [../README.md](../README.md)
- [../k8s/README.md](../k8s/README.md)