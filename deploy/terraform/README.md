# Sentinel Terraform — Helm release module

## What this is

A **Helm release wrapper** for the in-repo chart at `deploy/helm/sentinel`.

It installs Sentinel into an **existing** Kubernetes cluster. It does **not**
provision cloud infrastructure (no VPC, nodes, managed Postgres, DNS, or
registries).

## What this is not

| Claim | Reality |
| --- | --- |
| “Infrastructure as Code for Sentinel cloud” | False — cluster is BYO |
| “Terraform manages app secrets” | Forbidden — use a pre-created Kubernetes Secret |
| “terraform apply stands up AWS/GCP” | False — only a Helm release |

If you need cloud infra, add a separate provider-specific module; do not stretch
this root module beyond the Helm release.

## Secrets (required pattern)

Plaintext credentials must **not** be Terraform variables and must **not** enter
state.

1. Create the namespace (or let Helm create it).
2. Apply a Secret (see `secret.example.yaml`) via kubectl, External Secrets, or
   Sealed Secrets.
3. Point Terraform at that Secret name only:

```hcl
existing_secret_name = "sentinel-secrets"
```

The module always sets `secrets.create=false` and fails if a values override
tries to re-enable chart-managed secrets.

## Remote state

For anything beyond a disposable laptop cluster, copy `backend.tf.example` to
`backend.tf` and use an **encrypted** remote backend (S3+KMS, GCS CMEK, TFC).

## Usage

```bash
# 1) Ensure images exist on the cluster (see deploy/helm/sentinel/README.md)

# 2) Create the Secret (edit secret.example.yaml first)
kubectl create namespace sentinel --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sentinel apply -f secret.example.yaml

# 3) Configure non-secret inputs
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # set existing_secret_name, llm_provider, …

# 4) Plan / apply
terraform init
terraform plan
terraform apply
```

Port-forward for local access:

```bash
kubectl -n sentinel port-forward svc/sentinel-web 3002:3000 &
kubectl -n sentinel port-forward svc/sentinel-api 8080:8080 &
```

## CI checks

`scripts/check_terraform.sh` runs `fmt`, `validate`, and a representative
`terraform plan` against a disposable kind cluster without embedding secrets in
configuration.
