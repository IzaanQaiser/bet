# Media storage for MMS attachments. 30-day lifecycle rule per PRD §9.

resource "google_storage_bucket" "media" {
  name                        = "${var.project_id}-media"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # hackathon instance — allow teardown even with objects present

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# ingest-svc is the only writer — it's the only service that ever handles raw
# media (overview.md §3).
resource "google_storage_bucket_iam_member" "ingest_writes_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

# extractor-svc needs to *read* media bytes to pass to Gemini (step 11,
# multimodal) — granted now since the bucket already exists, even though
# extractor-svc itself isn't built until step 4.
resource "google_storage_bucket_iam_member" "extractor_reads_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.extractor.email}"
}
