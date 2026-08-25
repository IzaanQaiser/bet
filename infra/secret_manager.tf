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

# Web division Phase 3 — signs/verifies the short-lived registration token
# scripts/approve_waitlist.py mints. No value set here (same reasoning as
# the two secrets above): populate with
#   openssl rand -base64 32 | gcloud secrets versions add web-session-signing-key --data-file=-
# Phase 5's dashboard-svc will read this same secret to verify its own,
# separately-scoped session tokens — no per-service grant added yet
# (Phase 4 adds registration-svc's read access, once it has an endpoint
# that actually needs to verify a token this script minted).
resource "google_secret_manager_secret" "web_session_signing_key" {
  secret_id = "web-session-signing-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "registration_reads_signing_key" {
  secret_id = google_secret_manager_secret.web_session_signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.registration.email}"
}

# Web division Phase 5 — dashboard-svc verifies its own login-session
# tokens with this same key (a distinct `purpose` claim from
# registration-svc's tokens, obligation_engine_shared.tokens, so neither
# service's tokens can be replayed as the other's) — read-only, it never
# mints a registration-flow token.
resource "google_secret_manager_secret_iam_member" "dashboard_reads_signing_key" {
  secret_id = google_secret_manager_secret.web_session_signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dashboard.email}"
}

# Web division Phase 4 — the *second* OAuth client. Distinct from
# google-oauth-client-secret above: that one is bootstrap_oauth_token.py's
# Installed App client (a local CLI redirect flow), this one is a Web
# Application client with a real server-side redirect_uri
# (scripts/deploy.sh's OAUTH_REDIRECT_URI), created by hand per the plan's
# manual setup step 2 — Google doesn't expose OAuth-client creation as an
# API for a normal project. Populate with:
#   gcloud secrets versions add google-oauth-client-secret-web --data-file=-
resource "google_secret_manager_secret" "google_oauth_client_secret_web" {
  secret_id = "google-oauth-client-secret-web"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "registration_reads_oauth_client_secret_web" {
  secret_id = google_secret_manager_secret.google_oauth_client_secret_web.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.registration.email}"
}

# Web division Phase 4 — the Twilio Verify Service SID (plan's manual
# setup step 3). Unlike other Twilio identifiers in this project (Account
# SID, API Key SID — plain config, not secrets, infrastructure.md §4.1),
# the plan treats this one as a real secret; kept consistent with that
# rather than special-cased. Populate with:
#   gcloud secrets versions add twilio-verify-service-sid --data-file=-
resource "google_secret_manager_secret" "twilio_verify_service_sid" {
  secret_id = "twilio-verify-service-sid"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "registration_reads_verify_sid" {
  secret_id = google_secret_manager_secret.twilio_verify_service_sid.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.registration.email}"
}

# Web division Phase 5 — dashboard-svc reuses the same Verify Service for
# login OTP (a different flow's purpose, same Twilio Verify Service — one
# Service, many independent verification checks; Twilio doesn't require
# a separate Service per use case).
resource "google_secret_manager_secret_iam_member" "dashboard_reads_verify_sid" {
  secret_id = google_secret_manager_secret.twilio_verify_service_sid.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dashboard.email}"
}

# Twilio API Key — not in the original infrastructure.md plan, added during
# step 3 build. Distinct from twilio-auth-token: the Auth Token is required
# for webhook signature validation (no substitute), but outbound sends
# (resolver-svc, dispatcher-svc — overview.md's write matrix) use this
# instead, since an API key is independently revocable without touching the
# master Auth Token every other service's signature check depends on. The
# key SID (public, not a secret) is plain config, not stored here.
resource "google_secret_manager_secret" "twilio_api_key_secret" {
  secret_id = "twilio-api-key-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Phase 4 adds registration, Phase 5 adds dashboard, to this map — same
# credential (twilio-api-key-secret), also used for Twilio Verify API
# calls (registration's verify-start/verify-otp, dashboard's login OTP),
# not just plain SMS sends.
resource "google_secret_manager_secret_iam_member" "sms_senders_read_api_key" {
  for_each = {
    resolver     = google_service_account.resolver.email
    dispatcher   = google_service_account.dispatcher.email
    registration = google_service_account.registration.email
    dashboard    = google_service_account.dashboard.email
  }
  secret_id = google_secret_manager_secret.twilio_api_key_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value}"
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

# Web division Phase 4 — registration-svc's own oauth-callback creates
# exactly one kind of secret dynamically: user-refresh-token-{user_id}, the
# same naming bootstrap_oauth_token.py already uses. Unlike committer's
# unconditional project-wide admin grant above, this one is IAM-condition
# scoped to that name prefix specifically — the plan calls this out as
# "worth a real IAM-conditions review before shipping, not a broad
# secretAdmin role" (Phase 4 spec), so it gets the tighter treatment here
# rather than copying committer's precedent.
resource "google_project_iam_member" "registration_creates_user_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.registration.email}"

  condition {
    title       = "user-refresh-token-secrets-only"
    description = "registration-svc may only create/manage the per-user refresh-token secrets it mints at registration time, not any other secret in the project."
    expression  = "resource.name.startsWith(\"projects/${data.google_project.current.number}/secrets/user-refresh-token-\")"
  }
}
