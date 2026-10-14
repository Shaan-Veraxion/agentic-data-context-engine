# ADCE on Google Cloud (staging; production uses the same module with production variables).
# Neo4j runs in the cluster from its official Helm chart (no managed Neo4j on Google Cloud outside Aura).

terraform {
  required_version = ">= 1.9"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.7" }
    random = { source = "hashicorp/random", version = "~> 3.9" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  name   = "adce-${var.environment}"
  labels = { app = "adce", environment = var.environment }
  apis = ["container.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com",
    "aiplatform.googleapis.com", "artifactregistry.googleapis.com", "storage.googleapis.com",
  "servicenetworking.googleapis.com", "compute.googleapis.com", "iam.googleapis.com"]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ------------------------------------------------------------------ network (private Cloud SQL)
resource "google_compute_network" "vpc" {
  name                    = local.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "main" {
  name                     = local.name
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/20"
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.30.0.0/20"
  }
}

resource "google_compute_global_address" "private_services" {
  name          = "${local.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

# ------------------------------------------------------------------ GKE Autopilot
resource "google_container_cluster" "main" {
  name                = local.name
  location            = var.region
  enable_autopilot    = true
  network             = google_compute_network.vpc.id
  subnetwork          = google_compute_subnetwork.main.id
  deletion_protection = var.deletion_protection
  resource_labels     = local.labels
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  release_channel {
    channel = "REGULAR"
  }
}

# ------------------------------------------------------------------ images
resource "google_artifact_registry_repository" "images" {
  repository_id = "adce"
  location      = var.region
  format        = "DOCKER"
  labels        = local.labels
  depends_on    = [google_project_service.apis]
}

# ------------------------------------------------------------------ Postgres: fact log, review items, checkpoints
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "pg" {
  name                = local.name
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.deletion_protection
  settings {
    tier              = var.db_tier
    availability_type = var.environment == "production" ? "REGIONAL" : "ZONAL"
    user_labels       = local.labels
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }
  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "adce" {
  name     = "adce"
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_database" "keycloak" {
  name     = "keycloak"
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "adce" {
  name     = "adce"
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# ------------------------------------------------------------------ artifacts (S3-compatible access through HMAC)
resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project}-${local.name}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels
  versioning {
    enabled = true
  }
}

# ------------------------------------------------------------------ workload identity for the pods
resource "google_service_account" "workloads" {
  account_id   = "adce-workloads"
  display_name = "ADCE workloads (${var.environment})"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.workloads.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[${var.namespace}/adce]"
  depends_on         = [google_container_cluster.main]
}

resource "google_project_iam_member" "workloads" {
  for_each = toset(["roles/aiplatform.user", "roles/cloudsql.client"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.workloads.email}"
}

resource "google_storage_bucket_iam_member" "artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.workloads.email}"
}

resource "google_storage_hmac_key" "artifacts" {
  service_account_email = google_service_account.workloads.email
}

# ------------------------------------------------------------------ secrets (synced into the cluster as adce-secrets)
locals {
  secrets = {
    "adce-pg-dsn"        = "postgresql://adce:${random_password.db.result}@${google_sql_database_instance.pg.private_ip_address}:5432/adce"
    "adce-s3-access-key" = google_storage_hmac_key.artifacts.access_id
    "adce-s3-secret-key" = google_storage_hmac_key.artifacts.secret
  }
}

resource "google_secret_manager_secret" "app" {
  for_each  = local.secrets
  secret_id = "${each.key}-${var.environment}"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "app" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.app[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret_iam_member" "app" {
  for_each  = local.secrets
  secret_id = google_secret_manager_secret.app[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workloads.email}"
}
