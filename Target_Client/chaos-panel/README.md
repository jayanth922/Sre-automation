# Chaos Panel

This folder contains a lightweight browser-based control panel for inspecting and adjusting demo chaos settings.

## What It Is

The panel is a static HTML application that runs on port `8888`. It uses Chart.js and a small custom layout to present the target-client signal state and chaos controls in a quick, operator-friendly view.

## Why It Exists

The panel gives humans a fast way to see and influence the target environment without opening the Kubernetes manifests or editing service configuration by hand.

It is especially useful when you want to induce or observe an incident from the customer side while the platform is already running.

## Build And Run

The container image simply serves [index.html](index.html) with a Python HTTP server.

## Operational Role

This panel is the human-facing control surface for the noisy customer environment. It lets you keep the demo grounded in visible operational controls rather than invisible config flags.

## Related Docs

- [../README.md](../README.md)
- [../k8s/README.md](../k8s/README.md)