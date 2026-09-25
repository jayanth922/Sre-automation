variable "kubeconfig" {
  description = "Path to the kubeconfig file for an existing cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Optional kubeconfig context. Empty uses the current context."
  type        = string
  default     = ""
}

variable "namespace" {
  description = "Namespace for the Sentinel Helm release."
  type        = string
  default     = "sentinel"
}

variable "create_namespace" {
  description = "Create the namespace if it does not exist."
  type        = bool
  default     = true
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "sentinel"
}

variable "chart_path" {
  description = "Absolute or module-relative path to the Sentinel chart. Empty uses ../helm/sentinel."
  type        = string
  default     = ""
}

variable "llm_provider" {
  description = "LLM provider accepted by the chart: anthropic | gemini."
  type        = string
  default     = "anthropic"

  validation {
    condition = contains(
      ["anthropic", "gemini"],
      var.llm_provider
    )
    error_message = "llm_provider must be one of: anthropic, gemini."
  }
}

variable "existing_secret_name" {
  description = <<-EOT
    Name of a Kubernetes Secret that already exists (or will exist before apply)
    in the release namespace. Required keys match the Helm chart:
    SECRET_KEY, POSTGRES_PASSWORD, CREDENTIAL_ENCRYPTION_KEY, MCP_SERVICE_TOKEN,
    plus any provider keys you use (GROQ_API_KEY, ANTHROPIC_API_KEY, …).

    Create it with kubectl/ExternalSecrets/Sealed Secrets — never via Terraform
    variables — so plaintext credentials are not stored in Terraform state.
  EOT
  type        = string
}

variable "image_registry" {
  description = "Image registry prefix for Sentinel images (chart image.registry)."
  type        = string
  default     = "sentinel"
}

variable "image_tag" {
  description = "Image tag for Sentinel images."
  type        = string
  default     = "latest"
}

variable "image_pull_policy" {
  description = "imagePullPolicy for Sentinel pods."
  type        = string
  default     = "IfNotPresent"
}

variable "chart_values" {
  description = <<-EOT
    Additional non-secret Helm values merged into the release (maps/objects).
    Do not put credentials here. Example:

      chart_values = {
        ingress = { enabled = true, host = "sentinel.example.com", tls = true }
        api     = { replicas = 2 }
        features = { liveBusBackend = "redis" }
      }
  EOT
  type        = any
  default     = {}
}

variable "wait" {
  description = "Wait for Helm resources to become ready."
  type        = bool
  default     = true
}

variable "timeout" {
  description = "Helm operation timeout in seconds."
  type        = number
  default     = 600
}

variable "atomic" {
  description = "Atomic install/upgrade (rollback on failure)."
  type        = bool
  default     = true
}

variable "cleanup_on_fail" {
  description = "Delete new resources created during a failed release."
  type        = bool
  default     = true
}
