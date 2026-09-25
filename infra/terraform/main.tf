# =============================================================================
# Sentinel — Helm release module (NOT cloud infrastructure).
#
# This root module installs the in-repo Helm chart into an existing Kubernetes
# cluster. It does not provision VPCs, node pools, databases, registries, or
# DNS. Bring your own cluster and a Kubernetes Secret that already holds
# Sentinel credentials so plaintext secrets never enter Terraform state.
# =============================================================================

terraform {
  required_version = ">= 1.5.0, < 2.0.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }

  # Remote encrypted state is strongly recommended for any shared environment.
  # Copy backend.tf.example → backend.tf and configure your org's backend
  # (S3+KMS, GCS+CMEK, Terraform Cloud, etc.). Local state is only for
  # disposable single-operator clusters.
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig)
  config_context = var.kube_context != "" ? var.kube_context : null
}

provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kubeconfig)
    config_context = var.kube_context != "" ? var.kube_context : null
  }
}

locals {
  base_values = {
    secrets = {
      create         = false
      existingSecret = var.existing_secret_name
    }
    llm = {
      provider = var.llm_provider
    }
    image = {
      registry   = var.image_registry
      tag        = var.image_tag
      pullPolicy = var.image_pull_policy
    }
  }

  # Force secret handling: never chart-managed plaintext via Terraform.
  merged_values = merge(
    local.base_values,
    var.chart_values,
    {
      secrets = {
        create         = false
        existingSecret = var.existing_secret_name
      }
      llm = merge(
        local.base_values.llm,
        try(var.chart_values["llm"], {})
      )
      image = merge(
        local.base_values.image,
        try(var.chart_values["image"], {})
      )
    }
  )
}

resource "helm_release" "sentinel" {
  name             = var.release_name
  chart            = var.chart_path != "" ? var.chart_path : "${path.module}/../helm/sentinel"
  namespace        = var.namespace
  create_namespace = var.create_namespace
  wait             = var.wait
  timeout          = var.timeout
  atomic           = var.atomic
  cleanup_on_fail  = var.cleanup_on_fail

  values = [
    yamlencode(local.merged_values)
  ]

  lifecycle {
    precondition {
      condition     = try(local.merged_values.secrets.create, false) == false
      error_message = "This module forbids secrets.create=true — create a Kubernetes Secret out-of-band and set existing_secret_name so credentials never enter Terraform state."
    }
    precondition {
      condition     = length(trimspace(var.existing_secret_name)) > 0
      error_message = "existing_secret_name is required (name of a Secret already present in the target namespace)."
    }
    precondition {
      condition = contains(
        ["anthropic", "gemini"],
        var.llm_provider
      )
      error_message = "llm_provider must be one of: anthropic, gemini."
    }
  }
}
