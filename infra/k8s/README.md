# Kubernetes manifests

The base manifests deploy Sentinel into the `sentinel` namespace without a
bundled target workload or Meridian-specific defaults. They include the API,
worker, console, Postgres, Redis, Qdrant, MCP services, network policy, and
separate observer/actuator RBAC.

For a local cluster:

```bash
bash infra/k8s/install.sh
kubectl -n sentinel get pods -w
kubectl -n sentinel port-forward svc/sentinel-web 3002:3000
kubectl -n sentinel port-forward svc/sentinel-api 8080:8080
```

`install.sh` creates the ignored `infra/k8s/secret.yaml` from the example when
missing. Replace all example values before a real deployment. Anthropic is the
only supported model provider.

The Kubernetes MCP server uses the read-only `sentinel-observer` service
account. Mutating executor operations use `sentinel-actuator`, namespace
allowlists, policy/approval checks, and the runtime mutation gateway. No
kubeconfig is mounted into these pods.

For remote clusters, publish the images to a reachable registry and update the
manifest image references. Remove the installation with:

```bash
bash infra/k8s/install.sh --down
```

Validate the base manifests with
`bash scripts/ci/check_no_meridian_defaults.sh` and
`bash scripts/ci/check_kustomize.sh`.
