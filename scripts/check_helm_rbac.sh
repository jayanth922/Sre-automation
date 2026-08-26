#!/usr/bin/env bash
set -euo pipefail

chart_path="${1:-deploy/helm/sentinel}"
default_render="$(mktemp)"
cluster_render="$(mktemp)"
trap 'rm -f "$default_render" "$cluster_render"' EXIT

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets >"$default_render"

if grep -Eq '^kind: ClusterRole(Binding)?[[:space:]]*$' "$default_render"; then
  echo "default chart rendered cluster-wide RBAC" >&2
  exit 1
fi
grep -Eq '^kind: Role[[:space:]]*$' "$default_render"
grep -Eq '^kind: RoleBinding[[:space:]]*$' "$default_render"

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets \
  --set rbac.clusterWide.enabled=true >"$cluster_render"

grep -Eq '^kind: ClusterRole[[:space:]]*$' "$cluster_render"
grep -Eq '^kind: ClusterRoleBinding[[:space:]]*$' "$cluster_render"

echo "Helm RBAC scope checks passed"
