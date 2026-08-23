variable "project_id" {
  description = "GCP project ID this build deploys into. No default on purpose — never guess it."
  type        = string
}

variable "region" {
  description = "GCP region for all regional resources (Cloud Run, Cloud SQL, GCS, Artifact Registry)."
  type        = string
  default     = "us-central1"
}
