#!/usr/bin/env bash
# Manual /dispatch trigger for demo/judging (PRD §12.8, infrastructure.md
# §5) — same endpoint Cloud Scheduler hits, authenticated as the
# developer's own gcloud identity via an identity token rather than
# waiting for the 7am/1pm cron. This is what lets the capacity engine be
# shown live on camera.
#
# Usage: ./scripts/dispatch-now.sh
set -euo pipefail

PROJECT_ID="obligation-engine-hack"
REGION="us-central1"
ACCOUNT="waslyrideshare@gmail.com"

DISPATCHER_URL=$(gcloud run services describe dispatcher-svc \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)' --account="$ACCOUNT")

curl -sS -X POST \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --account="$ACCOUNT")" \
  "${DISPATCHER_URL}/dispatch"
echo
