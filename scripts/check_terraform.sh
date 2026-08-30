#!/usr/bin/env bash
# P05: terraform fmt/validate + representative plan (no secrets in TF config).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tf_dir="$root/deploy/terraform"

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required" >&2
  exit 1
fi

# Guard: module must not accept plaintext secret variables.
if grep -Eq 'variable "(secret_key|postgres_password|groq_api_key|anthropic_api_key)"' "$tf_dir"/*.tf; then
  echo "plaintext secret variables must not be declared in deploy/terraform" >&2
  exit 1
fi
if grep -Eq 'set_sensitive|secrets\.(secretKey|postgresPassword|groqApiKey)' "$tf_dir"/*.tf; then
  echo "Helm set_sensitive / chart secret keys must not be wired from Terraform" >&2
  exit 1
fi

echo "==> terraform fmt"
terraform -chdir="$tf_dir" fmt -check -recursive

echo "==> terraform init (no backend)"
terraform -chdir="$tf_dir" init -backend=false -input=false

echo "==> terraform validate"
terraform -chdir="$tf_dir" validate

# Representative plan against a disposable kind cluster when available.
if [[ "${SKIP_TERRAFORM_PLAN:-}" == "1" ]]; then
  echo "SKIP_TERRAFORM_PLAN=1 — skipping plan"
  exit 0
fi

if ! command -v kind >/dev/null 2>&1; then
  echo "kind not installed; running fmt/validate only (CI installs kind for plan)" >&2
  echo "Terraform fmt/validate passed (plan skipped)"
  exit 0
fi

cluster_name="${TF_KIND_CLUSTER:-sentinel-tf-ci}"
kubeconfig_path="$(mktemp)"
tfvars_path="$(mktemp)"
trap 'rm -f "$kubeconfig_path" "$tfvars_path"; kind delete cluster --name "$cluster_name" >/dev/null 2>&1 || true' EXIT

echo "==> kind cluster ($cluster_name)"
kind delete cluster --name "$cluster_name" >/dev/null 2>&1 || true
kind create cluster --name "$cluster_name" --wait 120s
kind get kubeconfig --name "$cluster_name" >"$kubeconfig_path"

echo "==> create namespace + non-production stub Secret (CI only)"
kubectl --kubeconfig="$kubeconfig_path" create namespace sentinel
kubectl --kubeconfig="$kubeconfig_path" -n sentinel create secret generic sentinel-secrets \
  --from-literal=SECRET_KEY="ci-only-not-for-prod" \
  --from-literal=POSTGRES_PASSWORD="ci-only-not-for-prod" \
  --from-literal=CREDENTIAL_ENCRYPTION_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" \
  --from-literal=MCP_SERVICE_TOKEN="ci-only-not-for-prod"

cat >"$tfvars_path" <<EOF
kubeconfig           = "$kubeconfig_path"
kube_context         = "kind-$cluster_name"
namespace            = "sentinel"
existing_secret_name = "sentinel-secrets"
llm_provider         = "groq"
image_registry       = "sentinel"
image_tag            = "ci"
wait                 = false
EOF

echo "==> terraform plan (representative, no secret values in tfvars)"
terraform -chdir="$tf_dir" init -backend=false -input=false >/dev/null
terraform -chdir="$tf_dir" plan -input=false -var-file="$tfvars_path" -out="$tf_dir/ci.tfplan" >/dev/null
# Ensure the plan file does not embed known stub secret payloads as set values
# (existingSecret name is fine; SECRET_KEY material must not appear).
if strings "$tf_dir/ci.tfplan" | grep -F "ci-only-not-for-prod" >/dev/null; then
  echo "plan unexpectedly contains secret material" >&2
  exit 1
fi
rm -f "$tf_dir/ci.tfplan"

echo "Terraform fmt/validate/plan checks passed"
