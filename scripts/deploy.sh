#!/usr/bin/env bash
# Deploys one service to Cloud Run. Application deploys are deliberately not
# in Terraform — the Cloud Run service, its run.invoker bindings, and its
# Pub/Sub push subscription all need each other in sequence, and image tags
# change every iteration, so this script owns that whole chain per service
# instead of churning Terraform state on every code change. See
# docs/architecture/infrastructure.md §6.
#
# Usage: ./scripts/deploy.sh <service-name>
# e.g.:  ./scripts/deploy.sh ingest-svc
set -euo pipefail

SERVICE="${1:?Usage: ./scripts/deploy.sh <service-name>}"
PROJECT_ID="obligation-engine-hack"
REGION="us-central1"
REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/obligation-engine"
IMAGE="${REPO}/${SERVICE}:latest"
SA="sa-${SERVICE%-svc}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Building and pushing ${IMAGE}..."
# --platform linux/amd64 explicitly: Cloud Run requires amd64, but a local
# build on Apple Silicon defaults to arm64.
docker build --platform linux/amd64 -f "services/${SERVICE}/Dockerfile" -t "$IMAGE" .
docker push "$IMAGE"

# Creates (or updates) a push subscription that invokes $SERVICE, and every
# IAM grant that needs — the pattern found while deploying extractor-svc
# (infrastructure.md §2.1's "Resolved gap" note): the push subscription's
# OIDC token is minted AS the consuming service's own SA, not the raw
# Pub/Sub push service agent, so Cloud Run's invoker check sees the same
# identity every other access-control story in this system already uses.
# Args: <subscription-name> <topic> <dlq-topic>
setup_push_subscription() {
  local subscription="$1" topic="$2" dlq_topic="$3"

  local service_url project_number pubsub_agent
  service_url=$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" --region="$REGION" \
    --format='value(status.url)' --account=waslyrideshare@gmail.com)
  project_number=$(gcloud projects describe "$PROJECT_ID" \
    --format='value(projectNumber)' --account=waslyrideshare@gmail.com)
  pubsub_agent="service-${project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  echo "Granting Pub/Sub push service agent tokenCreator on ${SA}..."
  gcloud iam service-accounts add-iam-policy-binding "$SA" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${pubsub_agent}" --role="roles/iam.serviceAccountTokenCreator" \
    --account=waslyrideshare@gmail.com

  echo "Granting ${SA} run.invoker on ${SERVICE}..."
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --project="$PROJECT_ID" --region="$REGION" \
    --member="serviceAccount:${SA}" --role="roles/run.invoker" \
    --account=waslyrideshare@gmail.com

  # Required for the dead-letter policy below: the push service agent needs
  # publish on the DLQ topic and subscribe on this subscription, or Pub/Sub
  # can't actually forward failed deliveries there.
  gcloud pubsub topics add-iam-policy-binding "$dlq_topic" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${pubsub_agent}" --role="roles/pubsub.publisher" \
    --account=waslyrideshare@gmail.com

  echo "Creating/updating ${subscription}..."
  if gcloud pubsub subscriptions describe "$subscription" \
    --project="$PROJECT_ID" --account=waslyrideshare@gmail.com >/dev/null 2>&1; then
    gcloud pubsub subscriptions update "$subscription" \
      --project="$PROJECT_ID" \
      --push-endpoint="${service_url}/pubsub/push" \
      --push-auth-service-account="${SA}" \
      --account=waslyrideshare@gmail.com
  else
    gcloud pubsub subscriptions create "$subscription" \
      --project="$PROJECT_ID" \
      --topic="$topic" \
      --push-endpoint="${service_url}/pubsub/push" \
      --push-auth-service-account="${SA}" \
      --dead-letter-topic="$dlq_topic" \
      --max-delivery-attempts=5 \
      --account=waslyrideshare@gmail.com
  fi

  gcloud pubsub subscriptions add-iam-policy-binding "$subscription" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${pubsub_agent}" --role="roles/pubsub.subscriber" \
    --account=waslyrideshare@gmail.com
}

case "$SERVICE" in
  ingest-svc)
    echo "Deploying ${SERVICE}..."
    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-ingest@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID}" \
      --set-secrets="TWILIO_AUTH_TOKEN=twilio-auth-token:latest" \
      --min-instances=0 \
      --allow-unauthenticated \
      --account=waslyrideshare@gmail.com
    ;;
  extractor-svc)
    echo "Deploying ${SERVICE}..."
    # No --allow-unauthenticated: only the Pub/Sub push service agent may
    # invoke this (infrastructure.md §2.1's "Invocable by" column). No
    # --add-cloudsql-instances either — sa-extractor has zero DB binding
    # (ADR 0003, infra/cloud_sql.tf's comment on the same line).
    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,VERTEX_LOCATION=global,GEMINI_MODEL=gemini-3.5-flash" \
      --min-instances=0 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    setup_push_subscription "items-raw-extractor-push" "items-raw" "items-raw-dlq"
    ;;
  resolver-svc)
    echo "Deploying ${SERVICE}..."
    # No --allow-unauthenticated, same invoker story as extractor-svc.
    # Unlike extractor-svc, sa-resolver does have a Cloud SQL binding
    # (infrastructure.md §2.1 — the stub writes items directly).
    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-resolver@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID}" \
      --min-instances=0 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    setup_push_subscription "items-extracted-resolver-push" "items-extracted" "items-extracted-dlq"
    ;;
  *)
    echo "No deploy config yet for ${SERVICE} — add a case in scripts/deploy.sh." >&2
    exit 1
    ;;
esac
