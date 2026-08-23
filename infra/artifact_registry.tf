resource "google_artifact_registry_repository" "services" {
  repository_id = "obligation-engine"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}
