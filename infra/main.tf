# Terraform-owned durable infrastructure. See docs/architecture/infrastructure.md
# for what this provisions and why. Application deploys (Cloud Run revisions/images)
# are NOT managed here — see scripts/deploy.sh, per infrastructure.md §6.

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
