# Sentinel Helm chart

Install Sentinel onto your own Kubernetes cluster. Everything is configurable in
`values.yaml` — bring your own LLM, your own MCP tool servers, and your own
Postgres/Redis if you run them, or let the chart deploy the basics.

## Prerequisites

- Kubernetes + `kubectl` + `helm` 3.x
- The Sentinel images available to your cluster. For local clusters, build them
  once (they'll be used directly with `imagePullPolicy: IfNotPresent`):

  ```bash
  # from the repo root
  docker build -f platform/Dockerfile           -t sentinel/api:latest  .
  docker build -f platform/Dockerfile.dashboard -t sentinel/web:latest  dashboard
  for s in k8s executor prometheus loki github github-exec runbooks; do
    case $s in
      k8s) d=k8s_real;; executor) d=executor_real;; prometheus) d=prometheus_real;;
      loki) d=loki_real;; github) d=github_real;; github-exec) d=github_exec;;
      runbooks) d=runbooks_local;;
    esac
    docker build -t sentinel/mcp-$s:latest edge_mcp_servers/mcp_servers/$d
  done
  ```

  For a remote cluster, retag/push to your registry and set `image.registry`.

## Install

```bash
helm install sentinel deploy/helm/sentinel \
  --namespace sentinel --create-namespace \
  --set secrets.secretKey=$(openssl rand -hex 32) \
  --set secrets.postgresPassword=$(openssl rand -hex 16)
```

Once it's up, open the console and register — the first sign-up creates the org and becomes its admin.

Then follow the notes printed on install (port-forward or ingress URL).

## Configure (highlights)

| Area | Keys |
|---|---|
| Bring-your-own LLM | `llm.provider=openai_compatible`, `llm.baseUrl`, `llm.model`, `secrets.llmApiKey` |
| Bring-your-own MCP | `mcp.extraServersJson`, `mcp.edge.enabled`, `mcp.edge.servers` |
| Bring-your-own datastores | `postgres.deploy=false` + `postgres.external.*`; `redis.deploy=false` + `redis.external.url`; `qdrant.deploy=false` + `qdrant.external.url` |
| Existing secret | `secrets.create=false`, `secrets.existingSecret=<name>` (keys: `SECRET_KEY`, `POSTGRES_PASSWORD`, `LLM_API_KEY`, …) |
| Private registry | `image.registry`, `imagePullSecrets` |
| Ingress | `ingress.enabled=true`, `ingress.className`, `ingress.host`, `ingress.tls`; set `web.wsBase=wss://<host>` |
| Storage | `postgres.storage`, `postgres.storageClass`, `qdrant.storage`, `qdrant.storageClass` |
| Sizing | `api.resources`, `web.resources`, `mcp.edge.resources`, `*.replicas` |

Validate your overrides before applying:

```bash
helm lint deploy/helm/sentinel -f my-values.yaml
helm template sentinel deploy/helm/sentinel -f my-values.yaml | kubectl apply --dry-run=client -f -
```

## Upgrade / uninstall

```bash
helm upgrade sentinel deploy/helm/sentinel -n sentinel -f my-values.yaml
helm uninstall sentinel -n sentinel
```

The agent reaches Kubernetes via in-cluster ServiceAccounts (read-only
`observer` for the k8s tool, `actuator` for scale/restart) — no kubeconfig is
mounted. Disable with `rbac.create=false` if you manage RBAC separately.
