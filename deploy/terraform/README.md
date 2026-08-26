# Sentinel — Terraform

Install the platform into a Kubernetes cluster as code, via the Helm provider.

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in secrets + your kube context
terraform init
terraform apply
```

Then port-forward as usual:

```bash
kubectl -n sentinel port-forward svc/sentinel-web 3002:3000 &
kubectl -n sentinel port-forward svc/sentinel-api 8080:8080 &
```

Prereqs: the `sentinel/*` images available to the cluster (build them once — see
the Helm chart README), Terraform ≥ 1.5, and a reachable kube context
(`kube_context` defaults to `orbstack`). `terraform destroy` removes it.
