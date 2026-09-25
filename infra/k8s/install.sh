#!/usr/bin/env bash
# =============================================================================
# One-command install for the Sentinel platform on Kubernetes.
#
#   ./infra/k8s/install.sh            build images + apply
#   ./infra/k8s/install.sh --apply    apply only (skip image build)
#   ./infra/k8s/install.sh --down     tear everything down
#
# Local clusters (OrbStack, Docker Desktop, kind) can use locally-built images
# directly (imagePullPolicy: IfNotPresent). For a remote cluster, push the
# sentinel/* images to a registry the cluster can pull from and adjust the image
# names in the manifests.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

if [[ "${1:-}" == "--down" ]]; then
  echo "▶ Deleting the sentinel namespace and all resources..."
  kubectl delete -k infra/k8s --ignore-not-found
  exit 0
fi

build_images() {
  echo "▶ Building images..."
  docker build -f infra/local/Dockerfile           -t sentinel/api:latest  .
  docker build -f infra/local/Dockerfile.dashboard -t sentinel/web:latest  apps/dashboard
  docker build -t sentinel/mcp-k8s:latest         services/edge_mcp_servers/mcp_servers/k8s_real
  docker build -t sentinel/mcp-executor:latest    services/edge_mcp_servers/mcp_servers/executor_real
  docker build -t sentinel/mcp-prometheus:latest  services/edge_mcp_servers/mcp_servers/prometheus_real
  docker build -t sentinel/mcp-loki:latest        services/edge_mcp_servers/mcp_servers/loki_real
  docker build -t sentinel/mcp-github:latest      services/edge_mcp_servers/mcp_servers/github_real
  docker build -t sentinel/mcp-github-exec:latest services/edge_mcp_servers/mcp_servers/github_exec
  docker build -t sentinel/mcp-runbooks:latest    services/edge_mcp_servers/mcp_servers/runbooks_notion
}

if [[ "${1:-}" != "--apply" ]]; then
  build_images
fi

if [[ ! -f infra/k8s/secret.yaml ]]; then
  echo "▶ Creating infra/k8s/secret.yaml from template — EDIT IT before real use."
  cp infra/k8s/secret.example.yaml infra/k8s/secret.yaml
fi

echo "▶ Applying manifests..."
kubectl apply -k infra/k8s

echo ""
echo "✅ Applied. Watch it come up:  kubectl -n sentinel get pods -w"
echo ""
echo "Then port-forward the console and the API (the browser needs both):"
echo "   kubectl -n sentinel port-forward svc/sentinel-web 3002:3000 &"
echo "   kubectl -n sentinel port-forward svc/sentinel-api 8080:8080 &"
echo ""
echo "Open http://localhost:3002 and register — the first sign-up creates the org and becomes admin."
