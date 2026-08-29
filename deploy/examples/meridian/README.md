# Meridian reference-client overlay
#
# Meridian is an **optional example client**, not part of the generic Sentinel
# platform. Base installs (`deploy/k8s`, Helm `values.yaml`, edge Compose) must
# run without Meridian. Apply this overlay only when the Meridian shop/workload
# namespace and signals MCP are actually deployed.

## Contract

| Concern | Value |
| --- | --- |
| Workload namespace | `meridian` |
| Signals MCP | `http://meridian-signals.meridian.svc.cluster.local:3000/sse` |
| Executor allowlist | `meridian` only |
| RBAC Role scope | Role/RoleBinding in namespace `meridian` |
| Platform namespace | unchanged (`sentinel`) |

Prerequisites (outside this repo):

1. Meridian workloads running in namespace `meridian`.
2. `meridian-signals` Service exposing the client signals MCP on port 3000.
3. Sentinel already installed (Helm or `deploy/k8s`).

## Helm

```bash
helm upgrade --install sentinel deploy/helm/sentinel -n sentinel \
  -f deploy/examples/meridian/helm-values.yaml \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-secrets
```

## Kustomize

```bash
kubectl apply -k deploy/examples/meridian
```

This patches the base ConfigMap + RBAC only; it does not reinstall datastores.

## Edge Compose (local Docker topology)

In `edge_mcp_servers/.env`:

```bash
EXECUTOR_ALLOWED_NAMESPACES=meridian
```

Register the signals MCP via platform `MCP_SERVERS_JSON` (or Helm
`mcp.extraServersJson`) using the URL in the contract table — Compose does not
start Meridian itself.
