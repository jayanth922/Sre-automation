# Sentinel on Kubernetes

One proper setup. The whole platform — API/agent runtime, web console, Postgres,
Redis, Qdrant, and the seven edge tool servers — runs in a single `sentinel`
namespace. There is **no bundled demo workload**: the platform connects to
whatever real infrastructure you configure per cluster in the console.

## Prerequisites

- A Kubernetes cluster and `kubectl` (local is fine: OrbStack, Docker Desktop,
  kind, minikube).
- Docker, to build the images.
- A model provider key. `LLM_PROVIDER` defaults to `groq` (free tier, reachable
  from inside the cluster). Put the key in the secret.

## Install (one command)

```bash
./deploy/k8s/install.sh
```

This builds all images, creates `deploy/k8s/secret.yaml` from the template (edit
it and re-apply for real use), and applies everything. Then:

```bash
kubectl -n sentinel get pods -w                                  # wait for Ready
kubectl -n sentinel port-forward svc/sentinel-web 3002:3000 &    # console
kubectl -n sentinel port-forward svc/sentinel-api 8080:8080 &    # API + WebSocket
```

Open http://localhost:3002 and **register**. The first person to sign up creates
the organization and becomes its admin; teammates join by registering with the
same organization name, and the admin manages roles under Team.

## Configure

- **Secrets** — `secret.yaml` (git-ignored): `SECRET_KEY`, `POSTGRES_PASSWORD`,
  and your model provider key (`GROQ_API_KEY`, etc.).
- **Config** — `config.yaml`: model choice, feature flags, and the in-cluster
  service DNS for the edge tool servers (already wired). `PROMETHEUS_URL` /
  `LOKI_URL` are blank by default — set them **per cluster** in the console
  (Settings), which is the generic, multi-tenant way.
- **Connect a cluster** — in the console, add your cluster's Prometheus/Loki
  endpoints and (optionally) your metric conventions under Settings. Point your
  Alertmanager webhook at `POST /api/v1/alerts/webhook` with the cluster token.

## How the agent reaches Kubernetes

No kubeconfig is mounted. `mcp-k8s` runs as the `sentinel-observer`
ServiceAccount (read-only) and `mcp-executor` as `sentinel-actuator`
(scale/restart), both via in-cluster config and the RBAC in `rbac.yaml`.

## Teardown

```bash
./deploy/k8s/install.sh --down
```

## Remote clusters

The manifests use locally-built `sentinel/*` images with
`imagePullPolicy: IfNotPresent`. For a remote cluster, push those images to a
registry the cluster can pull from and update the image names in the manifests.
