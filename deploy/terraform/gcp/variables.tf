variable "project" {
  description = "Google Cloud project id"
  type        = string
}

variable "region" {
  description = "Region for every resource (approved region for data residency)"
  type        = string
  default     = "europe-west1"
}

variable "environment" {
  description = "staging or production"
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production"
  }
}

variable "namespace" {
  description = "Kubernetes namespace the Helm release runs in (for workload identity)"
  type        = string
  default     = "adce"
}

variable "db_tier" {
  description = "Cloud SQL machine tier"
  type        = string
  default     = "db-custom-2-7680"
}

variable "deletion_protection" {
  description = "Protect the cluster and database from deletion"
  type        = bool
  default     = true
}
