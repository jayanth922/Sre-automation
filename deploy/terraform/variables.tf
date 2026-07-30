variable "kubeconfig" {
  description = "Path to the kubeconfig file."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context to target (e.g. orbstack, docker-desktop, kind-kind)."
  type        = string
  default     = "orbstack"
}

variable "namespace" {
  description = "Namespace to install the platform into."
  type        = string
  default     = "sentinel"
}

variable "llm_provider" {
  description = "LLM provider: groq | gemini | nvidia | ollama | openai_compatible."
  type        = string
  default     = "groq"
}

variable "secret_key" {
  description = "App secret key."
  type        = string
  sensitive   = true
}

variable "postgres_password" {
  description = "Postgres password (chart-managed Postgres)."
  type        = string
  sensitive   = true
}

variable "seed_admin_password" {
  description = "First admin login password."
  type        = string
  sensitive   = true
}

variable "groq_api_key" {
  description = "Groq API key (if llm_provider = groq)."
  type        = string
  sensitive   = true
  default     = ""
}
