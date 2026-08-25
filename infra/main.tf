# Terraform-owned durable infrastructure. See docs/architecture/infrastructure.md
# for what this provisions and why. Cloud Run services themselves (and their
# run.invoker bindings and push subscriptions) are NOT managed here — they're
# created imperatively per service in scripts/deploy.sh, per infrastructure.md §6.

terraform {
  required_version = ">= 1.9"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Secret Manager's actual resource names are project-number-based
# (projects/<number>/secrets/<id>), not project-id-based, confirmed via
# `gcloud secrets describe ... --format="value(name)"` — needed for the
# IAM condition in secret_manager.tf's registration_creates_user_secrets
# (a project-id string there would silently never match, making the
# grant a no-op).
data "google_project" "current" {
  project_id = var.project_id
}

# Required project APIs. infrastructure.md §1 — Vertex AI, Calendar, Gmail are
# APIs enabled here, not provisioned resources.
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
    "calendar-json.googleapis.com",
    "gmail.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}
