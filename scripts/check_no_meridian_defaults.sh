#!/usr/bin/env bash
# P06: base Sentinel manifests must not hardcode the Meridian reference client.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

check_no_meridian() {
  local path="$1"
  if command -v rg >/dev/null 2>&1; then
    if rg -n -i 'meridian' "$path" >/tmp/p06-meridian-hits.txt 2>/dev/null; then
      echo "Meridian coupling found in $path:" >&2
      cat /tmp/p06-meridian-hits.txt >&2
      fail=1
    fi
  else
    if grep -ni 'meridian' "$path" >/tmp/p06-meridian-hits.txt 2>/dev/null; then
      echo "Meridian coupling found in $path:" >&2
      cat /tmp/p06-meridian-hits.txt >&2
      fail=1
    fi
  fi
}

check_no_meridian "$root/deploy/k8s/config.yaml"
check_no_meridian "$root/deploy/k8s/rbac.yaml"
check_no_meridian "$root/deploy/helm/sentinel/values.yaml"

# Compose may mention Meridian only as documentation pointing at the example.
if grep -n 'EXECUTOR_ALLOWED_NAMESPACES=meridian' "$root/edge_mcp_servers/docker-compose.yaml" >/dev/null 2>&1; then
  echo "edge compose still hardcodes EXECUTOR_ALLOWED_NAMESPACES=meridian" >&2
  fail=1
fi

# Overlay must exist and mention meridian (positive control).
if ! grep -ri 'meridian' "$root/deploy/examples/meridian" >/dev/null; then
  echo "deploy/examples/meridian overlay missing Meridian contract content" >&2
  fail=1
fi

# Base kustomize render must not include meridian-signals.
if command -v kubectl >/dev/null 2>&1; then
  stub=0
  if [[ ! -f "$root/deploy/k8s/secret.yaml" ]]; then
    stub=1
    cat >"$root/deploy/k8s/secret.yaml" <<'EOF'
apiVersion: v1
kind: Secret
metadata: { name: sentinel-secrets, namespace: sentinel }
type: Opaque
stringData:
  SECRET_KEY: ci
  POSTGRES_PASSWORD: ci
  CREDENTIAL_ENCRYPTION_KEY: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
  MCP_SERVICE_TOKEN: ci
EOF
  fi
  render="$(mktemp)"
  kubectl kustomize "$root/deploy/k8s" >"$render"
  if grep -ni 'meridian' "$render" >/dev/null; then
    echo "base kustomize render still contains Meridian:" >&2
    grep -ni 'meridian' "$render" >&2 || true
    fail=1
  fi
  rm -f "$render"
  if [[ "$stub" -eq 1 ]]; then
    rm -f "$root/deploy/k8s/secret.yaml"
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "Meridian decoupling checks passed (base clean; overlay present)"
