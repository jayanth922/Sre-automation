#!/usr/bin/env bash
# P04: Helm lint + template checks for production-capable chart features.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chart_path="${1:-$root/deploy/helm/sentinel}"
render="$(mktemp)"
prod_render="$(mktemp)"
trap 'rm -f "$render" "$prod_render"' EXIT

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required" >&2
  exit 1
fi

helm lint "$chart_path"

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets \
  --set api.replicas=2 \
  --set features.liveBusBackend=redis >"$render"

grep -Eq '^kind: PodDisruptionBudget[[:space:]]*$' "$render"
grep -Eq '^kind: NetworkPolicy[[:space:]]*$' "$render"
grep -Eq 'name: redis-data' "$render"
grep -Eq 'appendonly' "$render"
grep -Eq 'LIVE_BUS_BACKEND: "redis"' "$render"
grep -Eq 'topologySpreadConstraints:' "$render"
grep -Eq 'startupProbe:' "$render"

# Multi-replica + memory bus must fail closed.
if helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets \
  --set api.replicas=2 \
  --set features.liveBusBackend=memory >/dev/null 2>"$prod_render"; then
  echo "expected fail for api.replicas>1 with memory live bus" >&2
  exit 1
fi
grep -qi 'liveBusBackend=redis' "$prod_render"

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets \
  -f "$chart_path/values-production.yaml" >"$prod_render"

grep -Eq '^kind: HorizontalPodAutoscaler[[:space:]]*$' "$prod_render"
grep -Eq '^kind: Ingress[[:space:]]*$' "$prod_render"
grep -Eq '^kind: PodDisruptionBudget[[:space:]]*$' "$prod_render"

echo "Helm production capability checks passed"
