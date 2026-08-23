# Static secrets only — the secret *containers*, not their values. Per-user
# refresh token secrets (user-refresh-token-{user_id}) are created dynamically
# at onboarding time by committer-svc's own code (infrastructure.md §6), not
# by Terraform.
#
# No value is set here for either secret — that requires a real Twilio
# account (step 3) and real Google OAuth client credentials, neither of which
# exist yet. Populate with:
#   gcloud secrets versions add twilio-auth-token --data-file=-
#   gcloud secrets versions add google-oauth-client-secret --data-file=-

resource "google_secret_manager_secret" "twilio_auth_token" {
  secret_id = "twilio-auth-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "google_oauth_client_secret" {
  secret_id = "google-oauth-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "ingest_reads_twilio_token" {
  secret_id = google_secret_manager_secret.twilio_auth_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingest.email}"
}

# committer-svc and dispatcher-svc get project-level secretAccessor rather
# than per-secret grants — deliberate simplification, infrastructure.md §4:
# they're the only two services with per-user refresh-token secrets to read,
# and per-secret IAM conditions add real complexity for no practical
# isolation benefit at this scale.
resource "google_project_iam_member" "oauth_secret_readers" {
  for_each = {
    committer  = google_service_account.committer.email
    dispatcher = google_service_account.dispatcher.email
  }
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${each.value}"
}

# committer-svc additionally creates per-user secrets at onboarding time
# (infrastructure.md §6) — needs create permission, not just read.
resource "google_project_iam_member" "committer_creates_user_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.committer.email}"
}
