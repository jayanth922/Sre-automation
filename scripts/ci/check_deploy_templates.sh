#!/usr/bin/env bash
# Validate Helm chart + Kustomize manifests. Terraform validate when tooling present.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

HELM_COMMON=(
  --namespace sentinel
  --set secrets.create=false
  --set secrets.existingSecret=sentinel-ci-secrets
)

echo "==> Helm lint + template (infra/helm/sentinel)"
helm lint infra/helm/sentinel
helm template sentinel infra/helm/sentinel "${HELM_COMMON[@]}" >/dev/null

echo "==> Existing RBAC / WS chart checks"
bash scripts/ci/check_helm_rbac.sh
bash scripts/ci/check_helm_ws.sh

echo "==> Kustomize build (infra/k8s)"
# secret.yaml is gitignored; materialize from the example for CI/local smoke only.
CLEANUP_SECRET=0
if [[ ! -f infra/k8s/secret.yaml ]]; then
  cp infra/k8s/secret.example.yaml infra/k8s/secret.yaml
  CLEANUP_SECRET=1
fi
kustomize_cleanup() {
  if [[ "$CLEANUP_SECRET" -eq 1 ]]; then
    rm -f infra/k8s/secret.yaml
  fi
}
trap kustomize_cleanup EXIT

if command -v kubectl >/dev/null 2>&1; then
  kubectl kustomize infra/k8s >/dev/null
elif command -v kustomize >/dev/null 2>&1; then
  kustomize build infra/k8s >/dev/null
else
  echo "ERROR: kubectl or kustomize required" >&2
  exit 1
fi
kustomize_cleanup
trap - EXIT

if [[ -f infra/terraform/main.tf ]]; then
  echo "==> Terraform fmt -check + validate (infra/terraform)"
  if command -v terraform >/dev/null 2>&1; then
    terraform -chdir=infra/terraform fmt -check
    # Provider download needs registry access (available in CI).
    terraform -chdir=infra/terraform init -backend=false -input=false
    terraform -chdir=infra/terraform validate
  else
    echo "ERROR: terraform is required for deploy template checks" >&2
    exit 1
  fi
fi

echo "Deploy template checks passed."
