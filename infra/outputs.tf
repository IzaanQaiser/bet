output "project_id" {
  value = var.project_id
}

output "cloud_sql_connection_name" {
  value = google_sql_database_instance.main.connection_name
}

output "media_bucket" {
  value = google_storage_bucket.media.name
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.services.repository_id}"
}

output "service_account_emails" {
  value = {
    ingest     = google_service_account.ingest.email
    extractor  = google_service_account.extractor.email
    resolver   = google_service_account.resolver.email
    committer  = google_service_account.committer.email
    dispatcher = google_service_account.dispatcher.email
  }
}

output "pubsub_topics" {
  value = [for t in google_pubsub_topic.pipeline : t.name]
}
