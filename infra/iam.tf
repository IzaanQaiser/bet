# Bindings that don't naturally belong in a resource-specific file.
# Cloud SQL client roles live in cloud_sql.tf, Pub/Sub publish roles in
# pubsub.tf, GCS roles in gcs.tf, Secret Manager roles in secret_manager.tf.
# What's left: Vertex AI access, granted only to the two services that
# actually call Gemini (agent-contracts.md §0 — extraction and clarification
# are the only two LLM call sites in the whole system).

resource "google_project_iam_member" "vertex_ai_users" {
  for_each = {
    extractor = google_service_account.extractor.email
    resolver  = google_service_account.resolver.email
  }
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${each.value}"
}
