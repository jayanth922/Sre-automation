# Kubernetes MCP Server

This service exposes Kubernetes cluster operations over MCP using the official Kubernetes Python client. It is the bridge between the agent runtime and the customer-target cluster state.

## Responsibilities

- Read the local kubeconfig from the mounted home directory.
- Resolve the Kubernetes API server through Docker Desktop or another reachable host context.
- Serve cluster, namespace, pod, deployment, event, and resource-related tools through FastMCP.
- Return structured cluster data that the platform can fold into an incident narrative.

## Why It Exists

The SRE agent needs a direct, structured way to inspect the customer cluster when investigating incidents. This server lets the graph query the live cluster without embedding Kubernetes client logic inside the main reasoning engine.

That separation matters because the graph can ask for cluster state as one evidence source among many rather than trying to understand Kubernetes object models inline.

## Configuration

- The compose stack mounts `${HOME}/.kube` read-only into the container.
- `KUBERNETES_API_SERVER_HOST` defaults to `host.docker.internal`.
- `HOST` defaults to `0.0.0.0`.
- The service is published on host port `4000`.

## Operational Notes

- The server assumes a reachable Kubernetes context from the host machine.
- The agent uses this server when it needs current cluster state, pod details, rollout information, or event history.
- The tool surface should stay focused on read-style evidence rather than becoming a cluster administration API.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)