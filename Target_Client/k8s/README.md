# Target Client Kubernetes Manifests

This folder contains the Kubernetes resources for the demo application and its monitoring setup.

## Contents

- [namespace.yaml](namespace.yaml) creates the `demo-app` namespace.
- [services.yaml](services.yaml) defines the API gateway, checkout, inventory, and load-generator deployments and services.
- `monitoring/` contains the Prometheus, Grafana, Alertmanager, and Loki resources used by the validation scripts.
- `chaos-panel.yaml` deploys the browser-based chaos control panel.

## How It Fits Together

The [../start.sh](../start.sh) script applies these manifests after it injects the built images into the Docker Desktop Kubernetes node. That script also performs a rollout restart so the pods pick up the newest images.

The manifests are the bridge between the source tree and the actual running demo. Without them, the services would just be local Docker images instead of a proper Kubernetes workload with monitoring annotations.

## Operational Notes

- The stack uses the `demo-app` namespace.
- The service manifests annotate the pods for Prometheus scraping.
- The monitoring resources are what the layer-1 validation scripts use to confirm telemetry is available.
- The namespace and service manifests assume the Docker Desktop Kubernetes environment used by the bootstrap script.

## Why This Folder Matters

This folder is where the noisy customer environment becomes a Kubernetes deployment rather than just a set of Python processes. The platform’s edge tools and observability workflows assume these resources exist and are reachable.

## Related Docs

- [../README.md](../README.md)
- [../testing/README.md](../testing/README.md)