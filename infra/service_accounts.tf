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

# Web division Phase 2 — first of two new public-facing services (see
# docs/architecture/infrastructure.md's web-division addendum). Only
# waitlist.join today; Phase 4 adds the OAuth/Twilio-Verify registration
# flow to this same service and same identity.
resource "google_service_account" "registration" {
  account_id   = "sa-registration"
  display_name = "registration-svc"
  depends_on   = [google_project_service.apis]
}

# Developer's own IAM identity, granted cloudsqlsuperuser for running
# migrations. Deliberate addition beyond docs/architecture/infrastructure.md's
# original service-account list: none of the four service-account Postgres
# users have schema-creation rights (Postgres 15 revokes CREATE on the public
# schema by default), and the built-in `postgres` user has no password set.
# IAM auth as the developer avoids managing a separate admin password/secret.

resource "google_sql_user" "developer_admin" {
  name     = "waslyrideshare@gmail.com"
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_USER"
}
