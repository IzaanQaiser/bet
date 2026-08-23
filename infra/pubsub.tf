# Topics only, in step 1. Push subscriptions need a live Cloud Run URL as their
# target, so each service's own build step (3, 4, 5, 6, 8) creates its
# subscription alongside deploying that service — not here. Subscribe-side
# Pub/Sub IAM and run.invoker bindings follow the same rule: added when the
# subscription/service they reference actually exists. See
# docs/architecture/overview.md §2/§4 for topology, §4 for the DLQ pattern.

locals {
  pipeline_topics = ["items-raw", "items-extracted", "items-confirmed"]
}

resource "google_pubsub_topic" "pipeline" {
  for_each = toset(local.pipeline_topics)
  name     = each.value
}

resource "google_pubsub_topic" "dlq" {
  for_each = toset(local.pipeline_topics)
  name     = "${each.value}-dlq"
}

# Publish-side IAM can be granted now (topic exists, publisher doesn't need a
# subscription to exist). Subscribe-side IAM is deferred per the note above.
resource "google_pubsub_topic_iam_member" "ingest_publishes_raw" {
  topic  = google_pubsub_topic.pipeline["items-raw"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_pubsub_topic_iam_member" "extractor_publishes_extracted" {
  topic  = google_pubsub_topic.pipeline["items-extracted"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.extractor.email}"
}

resource "google_pubsub_topic_iam_member" "resolver_publishes_confirmed" {
  topic  = google_pubsub_topic.pipeline["items-confirmed"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.resolver.email}"
}

# dispatcher-svc's publish permission on items-confirmed is narrower in spirit
# than committer's — it's the accept-a-suggestion path only (ADR 0003 applies
# the same confirm-before-write rule here as it does to resolver-svc). IAM
# itself can't express "only after parsing a Y reply" — that's enforced in
# code (state-machine.md §2.3), this grant just makes the publish possible.
resource "google_pubsub_topic_iam_member" "dispatcher_publishes_confirmed" {
  topic  = google_pubsub_topic.pipeline["items-confirmed"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.dispatcher.email}"
}
