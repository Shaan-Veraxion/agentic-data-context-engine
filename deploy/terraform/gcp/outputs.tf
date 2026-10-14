output "cluster" {
  value = google_container_cluster.main.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.images.repository_id}"
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "workload_service_account" {
  description = "Annotate the Kubernetes service account with this (values-staging.yaml)"
  value       = google_service_account.workloads.email
}

output "database_private_ip" {
  value = google_sql_database_instance.pg.private_ip_address
}

output "secrets" {
  description = "Secret Manager secrets to sync into the cluster secret adce-secrets"
  value       = [for s in google_secret_manager_secret.app : s.secret_id]
}
