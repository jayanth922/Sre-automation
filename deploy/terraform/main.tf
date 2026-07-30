# =============================================================================
# Sentinel — Infrastructure as Code.
#
# Installs the Sentinel platform (the Helm chart) into a Kubernetes cluster via
# the Terraform Helm provider. Targets a same-machine cluster by default
# (kubeconfig context), so `terraform apply` stands the whole platform up.
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

provider "kubernetes" {
  config_path    = var.kubeconfig
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig
    config_context = var.kube_context
  }
}

resource "helm_release" "sentinel" {
  name             = "sentinel"
  chart            = "${path.module}/../helm/sentinel"
  namespace        = var.namespace
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "llm.provider"
    value = var.llm_provider
  }

  set_sensitive {
    name  = "secrets.secretKey"
    value = var.secret_key
  }
  set_sensitive {
    name  = "secrets.postgresPassword"
    value = var.postgres_password
  }
  set_sensitive {
    name  = "secrets.seedAdminPassword"
    value = var.seed_admin_password
  }
  set_sensitive {
    name  = "secrets.groqApiKey"
    value = var.groq_api_key
  }
}
