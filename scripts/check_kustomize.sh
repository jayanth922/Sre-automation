#!/usr/bin/env bash
# P04: Kustomize build validation for deploy/k8s.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
k8s_path="${1:-$root/deploy/k8s}"
render="$(mktemp)"
trap 'rm -f "$render"' EXIT

# Prefer kubectl kustomize (no separate kustomize binary required).
if command -v kubectl >/dev/null 2>&1; then
  # secret.yaml is gitignored; synthesize a stub so the build can validate structure.
  stub_secret=0
  if [[ ! -f "$k8s_path/secret.yaml" ]]; then
    stub_secret=1
    cat >"$k8s_path/secret.yaml" <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: sentinel-secrets
  namespace: sentinel
type: Opaque
stringData:
  SECRET_KEY: "ci-validation-only"
  POSTGRES_PASSWORD: "ci-validation-only"
  CREDENTIAL_ENCRYPTION_KEY: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  MCP_SERVICE_TOKEN: "ci-validation-only"
EOF
  fi
  cleanup() {
    rm -f "$render"
    if [[ "$stub_secret" -eq 1 ]]; then
      rm -f "$k8s_path/secret.yaml"
    fi
  }
  trap cleanup EXIT

  kubectl kustomize "$k8s_path" >"$render"
elif command -v kustomize >/dev/null 2>&1; then
  kustomize build "$k8s_path" >"$render"
else
  echo "kubectl or kustomize is required" >&2
  exit 1
fi

grep -Eq '^kind: NetworkPolicy[[:space:]]*$' "$render"
grep -Eq '^kind: PodDisruptionBudget[[:space:]]*$' "$render"
grep -Eq 'name: redis-data' "$render"
grep -Eq 'LIVE_BUS_BACKEND: redis' "$render" || grep -Eq 'LIVE_BUS_BACKEND: "redis"' "$render"
grep -Eq 'replicas: 2' "$render"

echo "Kustomize validation checks passed"
