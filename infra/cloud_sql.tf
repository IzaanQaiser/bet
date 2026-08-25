# One Cloud SQL for PostgreSQL instance (ADR 0004). Table-level GRANTs for the
# app_* Postgres roles happen in migrations/0001_init.sql (step 2) — they need
# the tables to exist first, so they can't live here. This file only creates
# the instance, the database, and the login identities.
#
# Auth: Cloud SQL IAM database authentication — each service's own GCP
# service account doubles as its Postgres login role, no DB passwords to
# manage in Secret Manager. Decision made here, not previously specified in
# docs/architecture/infrastructure.md; consistent with that doc's "Secret
# Manager only for real secrets" spirit.
#
# Public IP, no authorized-networks allowlist: the Cloud SQL Auth Proxy
# (used by both Cloud Run and local dev, per docs/engineering/conventions.md)
# tunnels via the Cloud SQL Admin API and authenticates through IAM — it does
# not need a VPC connector or a private-IP-only instance. Chosen for setup
# simplicity within a 9-day build; the proxy is still the only thing that can
# reach it, not the open internet.

resource "google_sql_database_instance" "main" {
  name                = "obligation-engine-db"
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = false # hackathon instance — needs to be easy to tear down (infrastructure.md §8)

  settings {
    tier              = "db-f1-micro" # smallest shared-core tier (ADR 0004, infrastructure.md §1)
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_HDD"

    backup_configuration {
      enabled = false # throwaway demo data, cost control over durability
    }

    ip_configuration {
      ipv4_enabled = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "obligation_engine" {
  name     = "obligation_engine"
  instance = google_sql_database_instance.main.name
}

# IAM database users — one per service that needs Postgres access.
# sa-extractor is deliberately absent: no Cloud SQL binding at all (ADR 0003).
resource "google_sql_user" "ingest" {
  name     = trimsuffix(google_service_account.ingest.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "resolver" {
  name     = trimsuffix(google_service_account.resolver.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "committer" {
  name     = trimsuffix(google_service_account.committer.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "dispatcher" {
  name     = trimsuffix(google_service_account.dispatcher.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# Web division Phase 2 — registration-svc gets its own IAM database user,
# same pattern as the five pipeline services above.
resource "google_sql_user" "registration" {
  name     = trimsuffix(google_service_account.registration.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# roles/cloudsql.client — needed to open a connection via the Auth Proxy at all.
resource "google_project_iam_member" "cloudsql_client" {
  for_each = {
    ingest       = google_service_account.ingest.email
    resolver     = google_service_account.resolver.email
    committer    = google_service_account.committer.email
    dispatcher   = google_service_account.dispatcher.email
    registration = google_service_account.registration.email
  }
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${each.value}"
}

# roles/cloudsql.instanceUser — separate from cloudsql.client: this is what
# actually permits IAM database authentication as a specific DB user. Its
# absence surfaced as a real deploy-time bug (build step 3): the developer's
# own Owner-level access worked without it, masking that the four service
# accounts had no way to authenticate at all until this was added.
resource "google_project_iam_member" "cloudsql_instance_user" {
  for_each = {
    ingest       = google_service_account.ingest.email
    resolver     = google_service_account.resolver.email
    committer    = google_service_account.committer.email
    dispatcher   = google_service_account.dispatcher.email
    registration = google_service_account.registration.email
  }
  project = var.project_id
  role    = "roles/cloudsql.instanceUser"
  member  = "serviceAccount:${each.value}"
}
