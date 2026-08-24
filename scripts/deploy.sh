#!/usr/bin/env bash
# Deploys one service to Cloud Run. Application deploys are deliberately not
# in Terraform (infra/cloud_run.tf owns service *shells* only) — image tags
# change every iteration, and churning Terraform state on every code change
# is the wrong tool for that. See docs/architecture/infrastructure.md §6.
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
  *)
    echo "No deploy config yet for ${SERVICE} — add a case in scripts/deploy.sh." >&2
    exit 1
    ;;
esac
