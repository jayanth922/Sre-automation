output "release_name" {
  description = "Helm release name."
  value       = helm_release.sentinel.name
}

output "namespace" {
  description = "Kubernetes namespace of the release."
  value       = helm_release.sentinel.namespace
}

output "chart_version" {
  description = "Chart version that was deployed."
  value       = helm_release.sentinel.version
}

output "existing_secret_name" {
  description = "Kubernetes Secret name referenced by the chart (credentials stay outside Terraform state)."
  value       = var.existing_secret_name
}

output "scope" {
  description = "Honest scope statement for this module."
  value       = "Helm release wrapper only — does not provision cloud infrastructure."
}
