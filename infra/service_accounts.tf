# Five service accounts, one per Cloud Run service. Per-service IAM bindings
# live in iam.tf. See docs/architecture/infrastructure.md §2.1 for the full
# per-service grant table — this file only creates the identities.

resource "google_service_account" "ingest" {
  account_id   = "sa-ingest"
  display_name = "ingest-svc"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "extractor" {
  account_id   = "sa-extractor"
  display_name = "extractor-svc — no Cloud SQL binding, ever (ADR 0003)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "resolver" {
  account_id   = "sa-resolver"
  display_name = "resolver-svc"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "committer" {
  account_id   = "sa-committer"
  display_name = "committer-svc — also the dead-letter writer"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "dispatcher" {
  account_id   = "sa-dispatcher"
  display_name = "dispatcher-svc"
  depends_on   = [google_project_service.apis]
}
