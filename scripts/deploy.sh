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
# Not a secret — an OAuth client ID is a public identifier by design (same
# treatment as the Twilio Account SID, infrastructure.md §4.1). Only the
# client *secret* goes through Secret Manager (google-oauth-client-secret).
# Created via the manual bootstrap in infrastructure.md §4.
GOOGLE_OAUTH_CLIENT_ID="665100673712-md4toevjbouvfemojkne9ito237av8hk.apps.googleusercontent.com"
# Not secrets either, by the same Twilio credential-model reasoning — but
# kept out of every committed file regardless (this script included, not
# just service source), so nothing ever needs a source-scanner-aware
# reader to know that distinction. Required from your shell env instead,
# same pattern scripts/migrate.sh already uses for DATABASE_URL — find
# both in the Twilio Console (Account SID on the dashboard; the API Key
# SID wherever the paired twilio-api-key-secret was originally created).
: "${TWILIO_ACCOUNT_SID:?Set TWILIO_ACCOUNT_SID first — see the comment above this line}"
: "${TWILIO_API_KEY_SID:?Set TWILIO_API_KEY_SID first — see the comment above this line}"
# Web division (docs/design plan) — GitHub Pages, until the custom domain
# from the plan's manual setup step 1 is live. Two distinct values, not
# one: WEB_ORIGIN is a bare origin (scheme+host, no path) for CORS —
# browsers never send a path in an Origin header, so a path-suffixed
# value here would make every real cross-origin request from the site
# silently fail CORS. WEB_BASE_URL is the actual navigable site root,
# which *does* need /bet: this repo isn't named izaanqaiser.github.io, so
# a project-page GitHub Pages site always serves from
# izaanqaiser.github.io/<repo>/, confirmed via web/next.config.ts's
# matching `basePath` (found live, Phase 4's registration link 404'd
# without it). Update both (and re-run
# `./scripts/deploy.sh registration-svc`) once the custom domain is
# pointed and web/CNAME exists.
WEB_ORIGIN="https://izaanqaiser.github.io"
WEB_BASE_URL="https://izaanqaiser.github.io/bet"

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

  # ack-deadline=60: found via a real step-15 failure — the default 10s
  # (never explicitly set before this) is too tight for a real Gemini
  # call under load or cold start, especially the email-drafting
  # extraction call (slower than a plain classify). Pub/Sub redelivers
  # concurrently before the first attempt finishes, racing extractor-svc
  # (or resolver-svc's clarification call) on ADK's session id and
  # burning through real delivery attempts even when one eventually
  # succeeds — the same failure class as step 11's original finding, now
  # confirmed to sometimes exhaust all 5 attempts and reach dead_letters
  # for real, not just "wasteful but harmless." 60s covers realistic
  # Gemini/Calendar/Gmail latencies with real margin.
  echo "Creating/updating ${subscription}..."
  if gcloud pubsub subscriptions describe "$subscription" \
    --project="$PROJECT_ID" --account=waslyrideshare@gmail.com >/dev/null 2>&1; then
    gcloud pubsub subscriptions update "$subscription" \
      --project="$PROJECT_ID" \
      --push-endpoint="${service_url}/pubsub/push" \
      --push-auth-service-account="${SA}" \
      --ack-deadline=60 \
      --account=waslyrideshare@gmail.com
  else
    gcloud pubsub subscriptions create "$subscription" \
      --project="$PROJECT_ID" \
      --topic="$topic" \
      --push-endpoint="${service_url}/pubsub/push" \
      --push-auth-service-account="${SA}" \
      --dead-letter-topic="$dlq_topic" \
      --max-delivery-attempts=5 \
      --ack-deadline=60 \
      --account=waslyrideshare@gmail.com
  fi

  gcloud pubsub subscriptions add-iam-policy-binding "$subscription" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${pubsub_agent}" --role="roles/pubsub.subscriber" \
    --account=waslyrideshare@gmail.com
}

# Step 13: a push subscription on a .dlq topic itself — committer-svc's
# /pubsub/dlq, one subscription per .dlq topic since ?stage= is the
# simplest way to tell them apart (infrastructure.md §2.1's "Resolved
# gap" note on why committer-svc is the dead-letter writer). No nested
# dead-letter-policy here — a message that's already exhausted the real
# pipeline's retries has nowhere further to escalate to.
# Args: <subscription-name> <dlq-topic> <stage>
setup_dlq_subscription() {
  local subscription="$1" dlq_topic="$2" stage="$3"

  local service_url project_number pubsub_agent
  service_url=$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" --region="$REGION" \
    --format='value(status.url)' --account=waslyrideshare@gmail.com)
  project_number=$(gcloud projects describe "$PROJECT_ID" \
    --format='value(projectNumber)' --account=waslyrideshare@gmail.com)
  pubsub_agent="service-${project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  echo "Creating/updating ${subscription}..."
  if gcloud pubsub subscriptions describe "$subscription" \
    --project="$PROJECT_ID" --account=waslyrideshare@gmail.com >/dev/null 2>&1; then
    gcloud pubsub subscriptions update "$subscription" \
      --project="$PROJECT_ID" \
      --push-endpoint="${service_url}/pubsub/dlq?stage=${stage}" \
      --push-auth-service-account="${SA}" \
      --ack-deadline=60 \
      --account=waslyrideshare@gmail.com
  else
    gcloud pubsub subscriptions create "$subscription" \
      --project="$PROJECT_ID" \
      --topic="$dlq_topic" \
      --ack-deadline=60 \
      --push-endpoint="${service_url}/pubsub/dlq?stage=${stage}" \
      --push-auth-service-account="${SA}" \
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
    # RESOLVER_SVC_URL needs resolver-svc already deployed (step 5+) —
    # used for the step 9 inbound-reply routing forward call
    # (state-machine.md §4). The run.invoker grant that lets sa-ingest
    # actually call it lives in resolver-svc's own case below, matching
    # the "this resource's case fully describes its own finished IAM
    # state" pattern setup_push_subscription already uses. Step 11 adds
    # TWILIO_ACCOUNT_SID (plain config, not a secret — same treatment as
    # every other Twilio identifier, infrastructure.md §4.1) for
    # authenticating the MMS media download from Twilio. Step 14 adds
    # DISPATCHER_SVC_URL, same reasoning as RESOLVER_SVC_URL — the
    # suggestion-reply routing forward (state-machine.md §4 step 2); the
    # run.invoker grant lives in dispatcher-svc's own case below.
    RESOLVER_SVC_URL=$(gcloud run services describe resolver-svc \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)
    DISPATCHER_SVC_URL=$(gcloud run services describe dispatcher-svc \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)

    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-ingest@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},RESOLVER_SVC_URL=${RESOLVER_SVC_URL},DISPATCHER_SVC_URL=${DISPATCHER_SVC_URL},TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID}" \
      --set-secrets="TWILIO_AUTH_TOKEN=twilio-auth-token:latest" \
      --min-instances=1 \
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
      --min-instances=1 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    setup_push_subscription "items-raw-extractor-push" "items-raw" "items-raw-dlq"
    ;;
  resolver-svc)
    echo "Deploying ${SERVICE}..."
    # No --allow-unauthenticated, same invoker story as extractor-svc.
    # Unlike extractor-svc, sa-resolver does have a Cloud SQL binding
    # (infrastructure.md §2.1). Step 9 adds real Twilio sends (the
    # confirmation card + "Cancelled." — twilio-api-key-secret, same
    # credential dispatcher-svc uses) and a second invoker: sa-ingest,
    # for the synchronous inbound-reply forward (state-machine.md §4),
    # already anticipated in infrastructure.md §2.1's IAM matrix. Step 10
    # adds the real clarification Gemini call — same Vertex AI env vars
    # as extractor-svc (GEMINI_MODEL only via global location, §3's
    # "Resolved gap" note); sa-resolver already has aiplatform.user
    # (iam.tf), granted back in step 1 for exactly this.
    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-resolver@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,VERTEX_LOCATION=global,GEMINI_MODEL=gemini-3.5-flash,TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID},TWILIO_API_KEY_SID=${TWILIO_API_KEY_SID}" \
      --set-secrets="TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest" \
      --min-instances=1 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    echo "Granting sa-ingest run.invoker on ${SERVICE} (for routed replies)..."
    gcloud run services add-iam-policy-binding "$SERVICE" \
      --project="$PROJECT_ID" --region="$REGION" \
      --member="serviceAccount:sa-ingest@${PROJECT_ID}.iam.gserviceaccount.com" \
      --role="roles/run.invoker" \
      --account=waslyrideshare@gmail.com

    setup_push_subscription "items-extracted-resolver-push" "items-extracted" "items-extracted-dlq"
    ;;
  committer-svc)
    echo "Deploying ${SERVICE}..."
    # DISPATCHER_SVC_URL: committer-svc enqueues a Cloud Task per reminder
    # slot targeting dispatcher-svc's /dispatch/reminders/fire directly at
    # the exact reminder instant (real gap, found live — dispatcher-svc's
    # own /dispatch only runs twice a day, too coarse for same-day
    # reminders). The Cloud Tasks queue/IAM (sa-committer needs
    # cloudtasks.enqueuer on the "reminders" queue plus
    # iam.serviceAccountUser on sa-dispatcher, to mint the OIDC token the
    # task authenticates with) are provisioned once by hand, not by this
    # script — see docs/product/status.md.
    DISPATCHER_SVC_URL=$(gcloud run services describe dispatcher-svc \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)

    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-committer@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID},DISPATCHER_SVC_URL=${DISPATCHER_SVC_URL}" \
      --set-secrets="GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest" \
      --min-instances=0 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    setup_push_subscription "items-confirmed-committer-push" "items-confirmed" "items-confirmed-dlq"

    # Step 13: committer-svc also owns dead-letter persistence — one
    # push subscription per .dlq topic, all targeting the same service
    # (run.invoker for sa-committer on itself is already granted above).
    setup_dlq_subscription "items-raw-dlq-committer-push" "items-raw-dlq" "items-raw"
    setup_dlq_subscription "items-extracted-dlq-committer-push" "items-extracted-dlq" "items-extracted"
    setup_dlq_subscription "items-confirmed-dlq-committer-push" "items-confirmed-dlq" "items-confirmed"
    ;;
  dispatcher-svc)
    echo "Deploying ${SERVICE}..."
    # No Pub/Sub push subscription — dispatcher-svc is cron/manually
    # triggered (POST /dispatch), not a topic consumer. Twilio outbound
    # send needs the API Key secret (infrastructure.md §4.1); Calendar
    # read needs the same OAuth client as committer-svc.
    # COMMITTER_SVC_URL: ADR 0009's synchronous placeholder PUT/DELETE
    # call to committer-svc (capacity-engine.md §5.2). The Cloud Tasks
    # queue/IAM this also needs (sa-dispatcher: cloudtasks.enqueuer on
    # "reminders", run.invoker on committer-svc) are provisioned once by
    # hand, not by this script — see infrastructure.md §5.1.
    COMMITTER_SVC_URL=$(gcloud run services describe committer-svc \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)

    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-dispatcher@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID},TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID},TWILIO_API_KEY_SID=${TWILIO_API_KEY_SID},COMMITTER_SVC_URL=${COMMITTER_SVC_URL}" \
      --set-secrets="GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest,TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest" \
      --min-instances=0 \
      --no-allow-unauthenticated \
      --account=waslyrideshare@gmail.com

    SERVICE_URL=$(gcloud run services describe "$SERVICE" \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)

    # DISPATCHER_SVC_URL: tasks_client.py's own fire-task enqueue targets
    # this service's own URL (ADR 0009 — dispatcher-svc enqueueing a
    # Cloud Task at itself for the first time). Set via update, not the
    # initial deploy above, since the URL isn't known until the service
    # exists — same "needs itself to exist first" ordering every other
    # URL-dependent step in this script already has.
    echo "Setting DISPATCHER_SVC_URL=${SERVICE_URL} on ${SERVICE}..."
    gcloud run services update "$SERVICE" \
      --project="$PROJECT_ID" --region="$REGION" \
      --update-env-vars="DISPATCHER_SVC_URL=${SERVICE_URL}" \
      --account=waslyrideshare@gmail.com

    # Cloud Scheduler jobs need the live URL, same "needs the service to
    # exist first" reasoning as setup_push_subscription — not Terraform,
    # per infrastructure.md §6's "Resolved gap" note (extended here to
    # cover Scheduler jobs, not just push subscriptions). OIDC identity is
    # sa-dispatcher itself, same pattern as the push subscriptions: mint
    # the token as the consuming service's own SA, not a separate one.
    echo "Granting ${SA} run.invoker on ${SERVICE} (for Cloud Scheduler)..."
    gcloud run services add-iam-policy-binding "$SERVICE" \
      --project="$PROJECT_ID" --region="$REGION" \
      --member="serviceAccount:${SA}" --role="roles/run.invoker" \
      --account=waslyrideshare@gmail.com

    # Step 14: the routed suggestion-reply forward (state-machine.md §4
    # step 2), same pattern as sa-ingest -> resolver-svc.
    echo "Granting sa-ingest run.invoker on ${SERVICE} (for routed suggestion replies)..."
    gcloud run services add-iam-policy-binding "$SERVICE" \
      --project="$PROJECT_ID" --region="$REGION" \
      --member="serviceAccount:sa-ingest@${PROJECT_ID}.iam.gserviceaccount.com" \
      --role="roles/run.invoker" \
      --account=waslyrideshare@gmail.com

    # dispatch-daily/dispatch-midday hit the full /dispatch (real Calendar
    # reads, capacity snapshots + suggestions) — twice a day is the right
    # cadence there (infrastructure.md §4's quota assumption). Real gap,
    # found live: that cadence meant a same-day reminder (e.g. 9pm) never
    # got checked until the next day's 7am run, hours too late. The actual
    # fix is committer-svc scheduling a precise Cloud Task per reminder
    # (main.py's _enqueue_reminder_task, firing /dispatch/reminders/fire
    # directly at the exact instant) — polling isn't the primary mechanism.
    # dispatch-reminders hits the cheap /dispatch/reminders (no Calendar
    # reads) on a deliberately infrequent cadence, purely as a fallback
    # for whatever the precise path might miss.
    for job in dispatch-daily:"0 7 * * *":/dispatch dispatch-midday:"0 13 * * *":/dispatch \
      dispatch-reminders:"*/30 * * * *":/dispatch/reminders; do
      job_name="${job%%:*}"
      job_rest="${job#*:}"
      job_cron="${job_rest%%:*}"
      job_path="${job_rest#*:}"
      echo "Creating/updating ${job_name}..."
      if gcloud scheduler jobs describe "$job_name" \
        --project="$PROJECT_ID" --location="$REGION" --account=waslyrideshare@gmail.com >/dev/null 2>&1; then
        gcloud scheduler jobs update http "$job_name" \
          --project="$PROJECT_ID" --location="$REGION" \
          --schedule="$job_cron" --time-zone="America/Toronto" \
          --uri="${SERVICE_URL}${job_path}" --http-method=POST \
          --oidc-service-account-email="$SA" \
          --account=waslyrideshare@gmail.com
      else
        gcloud scheduler jobs create http "$job_name" \
          --project="$PROJECT_ID" --location="$REGION" \
          --schedule="$job_cron" --time-zone="America/Toronto" \
          --uri="${SERVICE_URL}${job_path}" --http-method=POST \
          --oidc-service-account-email="$SA" \
          --account=waslyrideshare@gmail.com
      fi
    done
    ;;
  registration-svc)
    echo "Deploying ${SERVICE}..."
    # First of the web division's two public services (docs/design plan
    # Phase 2). --allow-unauthenticated same as ingest-svc — the only other
    # public-facing service — but the request itself carries no external
    # signature to validate (unlike Twilio's), so CORS + the in-app rate
    # limit are what stand between this and the open internet, not IAM.
    # min-instances=0: unlike ingest-svc, a cold start here just makes one
    # waitlist submission slightly slower, not a missed webhook.
    #
    # Phase 4 adds the registration-completion env vars/secrets below.
    # GOOGLE_OAUTH_CLIENT_ID_WEB is the Web Application client from the
    # plan's manual setup step 2 (Console-only — Google doesn't expose
    # OAuth-client creation as an API) — fill in once created.
    # OAUTH_REDIRECT_URI is Cloud Run's own default URL format
    # (https://<service>-<project-number>.<region>.run.app), which is
    # deterministic ahead of a first deploy, not a random per-revision
    # hash — safe to register in the Google Console before this service
    # exists. Update it once the plan's custom-domain step 1 is live.
    GOOGLE_OAUTH_CLIENT_ID_WEB="${GOOGLE_OAUTH_CLIENT_ID_WEB:-}"
    OAUTH_REDIRECT_URI="https://registration-svc-665100673712.us-central1.run.app/register/oauth-callback"

    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-registration@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},ALLOWED_ORIGINS=${WEB_ORIGIN},WEB_BASE_URL=${WEB_BASE_URL},GOOGLE_OAUTH_CLIENT_ID_WEB=${GOOGLE_OAUTH_CLIENT_ID_WEB},OAUTH_REDIRECT_URI=${OAUTH_REDIRECT_URI},TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID},TWILIO_API_KEY_SID=${TWILIO_API_KEY_SID}" \
      --set-secrets="TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest,TWILIO_VERIFY_SERVICE_SID=twilio-verify-service-sid:latest,WEB_SESSION_SIGNING_KEY=web-session-signing-key:latest,GOOGLE_OAUTH_CLIENT_SECRET_WEB=google-oauth-client-secret-web:latest" \
      --min-instances=0 \
      --allow-unauthenticated \
      --account=waslyrideshare@gmail.com
    ;;
  dashboard-svc)
    echo "Deploying ${SERVICE}..."
    # Second of the web division's two public services (web division
    # Phase 5) — same shape as registration-svc: --allow-unauthenticated,
    # auth enforced at the app layer (session JWT), not IAM.
    # min-instances=0, same cost reasoning as registration-svc.
    # GOOGLE_OAUTH_CLIENT_ID/SECRET: the Installed App client
    # (bootstrap_oauth_token.py's, same as committer-svc/dispatcher-svc)
    # for DELETE /me/items/{id}'s best-effort Calendar delete — refreshing
    # a token requires the *same* client that originally issued it. Known
    # gap, not solved here: a user onboarded through Phase 4's web flow
    # instead got their refresh token from the Web Application client
    # (google-oauth-client-secret-web); this endpoint would fail to
    # refresh that token today. Only the original bootstrap-script demo
    # user exists right now, so this isn't hit in practice yet — revisit
    # once a real Phase-4-registered user needs this endpoint.
    # DISPATCHER_SVC_URL: PATCH /me/profile enqueues a Cloud Task at
    # dispatcher-svc's POST /users/{user_id}/next-fit whenever working
    # hours change (real gap, found live — a stale next_fit_start
    # otherwise persisted until the next twice-daily sweep). Same
    # committer-svc-pattern Cloud Tasks queue/IAM (sa-dashboard needs
    # cloudtasks.enqueuer on "reminders" plus iam.serviceAccountUser on
    # sa-dispatcher), provisioned once by hand — see docs/product/status.md.
    DISPATCHER_SVC_URL=$(gcloud run services describe dispatcher-svc \
      --project="$PROJECT_ID" --region="$REGION" \
      --format='value(status.url)' --account=waslyrideshare@gmail.com)

    gcloud run deploy "$SERVICE" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --image="$IMAGE" \
      --service-account="$SA" \
      --add-cloudsql-instances="${PROJECT_ID}:${REGION}:obligation-engine-db" \
      --set-env-vars="DB_USER=sa-dashboard@${PROJECT_ID}.iam,INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:obligation-engine-db,GCP_PROJECT_ID=${PROJECT_ID},ALLOWED_ORIGINS=${WEB_ORIGIN},TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID},TWILIO_API_KEY_SID=${TWILIO_API_KEY_SID},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID},DISPATCHER_SVC_URL=${DISPATCHER_SVC_URL}" \
      --set-secrets="TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest,TWILIO_VERIFY_SERVICE_SID=twilio-verify-service-sid:latest,WEB_SESSION_SIGNING_KEY=web-session-signing-key:latest,GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest" \
      --min-instances=0 \
      --allow-unauthenticated \
      --account=waslyrideshare@gmail.com
    ;;
  *)
    echo "No deploy config yet for ${SERVICE} — add a case in scripts/deploy.sh." >&2
    exit 1
    ;;
esac
