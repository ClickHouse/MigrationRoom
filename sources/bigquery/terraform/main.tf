terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4.0"
    }
  }
}

# ── Operating modes ───────────────────────────────────────────────────
# 1. Attach mode (default): set var.existing_project_id, Terraform
#    creates dataset + service account + (optional) GCS bucket inside it.
# 2. Greenfield mode: leave var.existing_project_id null and set
#    var.billing_account_id — Terraform also creates the project under
#    the given billing account and enables the BigQuery + Storage APIs.

locals {
  greenfield = var.existing_project_id == null
  project_id = local.greenfield ? google_project.demo[0].project_id : var.existing_project_id
}

provider "google" {
  # In greenfield mode the provider must be authenticated as a user (or
  # CI principal) with permission to create projects in the given
  # billing account. In attach mode any user with BigQuery Admin +
  # IAM Admin on the project is sufficient.
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 3
}

# ── Project (greenfield mode only) ────────────────────────────────────
resource "google_project" "demo" {
  count           = local.greenfield ? 1 : 0
  name            = "AI Migration Demo"
  project_id      = "ai-migration-${random_id.suffix.hex}"
  billing_account = var.billing_account_id
  deletion_policy = "DELETE"
}

resource "google_project_service" "bigquery" {
  count   = local.greenfield ? 1 : 0
  project = local.project_id
  service = "bigquery.googleapis.com"

  disable_on_destroy = false
  depends_on         = [google_project.demo]
}

resource "google_project_service" "storage" {
  count   = local.greenfield && var.create_staging_bucket ? 1 : 0
  project = local.project_id
  service = "storage.googleapis.com"

  disable_on_destroy = false
  depends_on         = [google_project.demo]
}

# ── BigQuery dataset ──────────────────────────────────────────────────
resource "google_bigquery_dataset" "demo" {
  project                    = local.project_id
  dataset_id                 = var.dataset_name
  location                   = var.location
  description                = "MigrationRoom demo: TPC-H + BigQuery-specific augmentations."
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

# ── Service account for the bigquery-source MCP + migration-runner ────
resource "google_service_account" "demo" {
  project      = local.project_id
  account_id   = "migration-demo-sa"
  display_name = "MigrationRoom demo SA"
  description  = "Read access to ${var.dataset_name} for the BigQuery migration demo."
}

resource "google_project_iam_member" "demo_data_editor" {
  project = local.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.demo.email}"
}

resource "google_project_iam_member" "demo_job_user" {
  project = local.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.demo.email}"
}

resource "google_service_account_key" "demo" {
  service_account_id = google_service_account.demo.name
  public_key_type    = "TYPE_X509_PEM_FILE"
}

# Write the SA JSON key to ../../../secrets/gcp-key.json (sensitive!).
# The bigquery-source MCP container mounts this path at /secrets/gcp-key.json
# via docker-compose.yml.
resource "local_sensitive_file" "key" {
  content_base64  = google_service_account_key.demo.private_key
  filename        = "${path.module}/../../../secrets/gcp-key.json"
  file_permission = "0600"
}

# ── GCS staging bucket (optional) ─────────────────────────────────────
resource "google_storage_bucket" "staging" {
  count    = var.create_staging_bucket ? 1 : 0
  project  = local.project_id
  name     = "ai-migration-staging-${random_id.suffix.hex}"
  location = var.location

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.storage]
}

resource "google_storage_bucket_iam_member" "staging_admin" {
  count  = var.create_staging_bucket ? 1 : 0
  bucket = google_storage_bucket.staging[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.demo.email}"
}

# HMAC keys for the migration SA. ClickHouse Cloud's `gcs()` table
# function authenticates via HMAC (it does not accept SA JSON), so the
# staging path needs these in addition to the SA itself. One pair per
# bucket is fine — the same keys work for any object the SA can see.
resource "google_storage_hmac_key" "staging" {
  count                 = var.create_staging_bucket ? 1 : 0
  project               = local.project_id
  service_account_email = google_service_account.demo.email

  depends_on = [google_project_service.storage]
}
