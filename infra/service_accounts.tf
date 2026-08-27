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

# Web division Phase 5 — dashboard-svc's identity. Read-only across the
# pipeline's data, scoped per-caller in application code (migrations/
# 0011_dashboard_grants.sql's comment).
resource "google_service_account" "dashboard" {
  account_id   = "sa-dashboard"
  display_name = "dashboard-svc"
  depends_on   = [google_project_service.apis]
}

# Two-way Calendar sync — the reverse of the direction that already
# worked (dashboard/ADR-0009 delete-here -> real Calendar delete). Only
# ever reads Calendar (events.watch/events.list) and writes Postgres;
# never calls the Calendar write API, so it never touches ADR 0003/0009's
# single-writer boundary. See docs/architecture/overview.md's service
# topology for why this is its own service, not folded into
# dispatcher-svc: its /webhook route must be publicly reachable
# (Google's push notifications carry no Cloud Run IAM token), and that
# toggle is service-wide, not per-route.
resource "google_service_account" "calendar_sync" {
  account_id   = "sa-calendar-sync"
  display_name = "calendar-sync-svc"
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
