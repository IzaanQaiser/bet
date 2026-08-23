# Infrastructure

Sixth and final doc in the architecture set — see `overview.md` §0. Closes out every remaining "→ `infrastructure.md`" pointer left by the other five docs. Where those docs described write-access *boundaries*, this doc is what actually enforces them in GCP — every claim in `overview.md` §3's matrix has to resolve to a concrete IAM binding here, or it isn't real.

---

## 1. Resource inventory

| Resource | Detail |
|---|---|
| Cloud Run services | `ingest-svc`, `extractor-svc`, `resolver-svc`, `committer-svc`, `dispatcher-svc` — one revision each, `min-instances=0` everywhere (PRD §9 cost control) |
| Cloud SQL | One Postgres instance, smallest shared-core tier, `pgvector` enabled (ADR 0004) |
| Pub/Sub topics | `items.raw`, `items.extracted`, `items.confirmed`, plus `items.raw.dlq`, `items.extracted.dlq`, `items.confirmed.dlq` |
| Pub/Sub subscriptions | One push subscription per topic per consuming service, `items.*` subscriptions configured with a dead-letter policy (`max_delivery_attempts=3`) pointing at the matching `.dlq` topic |
| Cloud Scheduler | Two jobs — `dispatch-daily` (07:00), `dispatch-midday` — both push `POST /dispatch` on `dispatcher-svc` |
| GCS bucket | Media storage, `raw_media_uri` targets; lifecycle rule deletes objects after 30 days (PRD §9) |
| Secret Manager | `twilio-auth-token`, `google-oauth-client-secret`, one `user-refresh-token-{user_id}` secret per onboarded user |
| Artifact Registry | One repo, container images for all five services |
| Service accounts | Five, one per Cloud Run service — see §2 |

Vertex AI and Google Calendar/Gmail are not provisioned resources — they're APIs enabled on the project (`aiplatform.googleapis.com`, `calendar-json.googleapis.com`, `gmail.googleapis.com`) plus IAM/OAuth grants covered below.

---

## 2. Service accounts and IAM — the enforcement layer for `overview.md` §3

Two IAM layers matter here and they enforce different things:
- **GCP IAM** (project/resource-level roles) — enforces which *external systems and Pub/Sub topics* each service can touch. This is where ADR 0003's "extractor-svc has no Calendar scope, no Gmail scope, no DB write role" is actually true or false.
- **Postgres GRANTs** (table-level, via per-service DB roles) — enforces which *tables* each service can read/write once connected to Cloud SQL. Postgres has no native concept of "only rows in state X" — that invariant (e.g. "resolver only writes items in `CLARIFYING`/`CONFIRMED`") is enforced by the state-machine code, not the database. Said plainly here rather than implied: GCP IAM is a hard boundary; the state-machine rule inside a table a service *can* write is a software invariant, tested but not database-enforced.

### 2.1 Per-service GCP IAM

| Service account | Pub/Sub | Cloud SQL (`roles/cloudsql.client`) | Secret Manager | Vertex AI | External APIs | Invocable by |
|---|---|---|---|---|---|---|
| `sa-ingest` | publish: `items.raw` · subscribe: none (HTTP push target, not a subscriber) | yes | `twilio-auth-token` (accessor) | none | none | public (Twilio; validated by request-signature check in code, not IAM) |
| `sa-extractor` | subscribe: `items.raw` · publish: `items.extracted` | **none — no binding at all** | none | `roles/aiplatform.user` | none | Pub/Sub push service agent only |
| `sa-resolver` | subscribe: `items.extracted` · publish: `items.confirmed` | yes | none | `roles/aiplatform.user` | none | Pub/Sub push service agent, `sa-ingest` (`roles/run.invoker`, for routed replies) |
| `sa-committer` | subscribe: `items.confirmed`, `items.raw.dlq`, `items.extracted.dlq`, `items.confirmed.dlq` · publish: none | yes | `google-oauth-client-secret`, `user-refresh-token-*` (accessor) | none | Calendar (write), Gmail (send) | Pub/Sub push service agent only |
| `sa-dispatcher` | subscribe: none (cron-triggered) · publish: `items.confirmed` (accept-path only) | yes | `google-oauth-client-secret`, `user-refresh-token-*` (accessor) | none | Calendar (read only) | Cloud Scheduler (`roles/run.invoker`), `sa-ingest` (routed suggestion replies), developer (manual trigger, §5) |

Every Pub/Sub grant above is bound **at the topic or subscription resource**, not project-wide — a project-wide `roles/pubsub.publisher` on `sa-extractor` would let it publish to `items.confirmed` directly and quietly defeat the entire confirm-before-write story (ADR 0003). This is non-negotiable in the Terraform: `google_pubsub_topic_iam_member` / `google_pubsub_subscription_iam_member`, resource-scoped, never `google_project_iam_member` for Pub/Sub roles.

**`sa-extractor` has no Cloud SQL binding, full stop** — it never even opens a connection. This is the cleanest, most literal reading of "the extractor never holds write credentials" (PRD §15): not scoped-down DB access, *no* DB access.

**Resolved gap: who writes `dead_letters` rows?** Pub/Sub's dead-letter policy only forwards a failed message to a `.dlq` topic — nothing writes the `dead_letters` table row automatically, and `sa-extractor` (which has no DB access) can't be the one to do it for its own failures. **Decision made here:** `committer-svc` doubles as the dead-letter writer, with an additional subscription to all three `.dlq` topics alongside its normal `items.confirmed` subscription. It's already the service with the broadest legitimate DB access on the write side, so one more narrow grant (`INSERT` on `dead_letters`) is a small addition, not a new trust boundary — reusing an existing service beats standing up a sixth Cloud Run deployment for one INSERT statement. `overview.md` §2/§3 should be read as amended: `committer-svc`'s responsibilities include dead-letter persistence, and its Pub/Sub reads include the three `.dlq` topics.

### 2.2 Postgres roles (table-level GRANTs)

One Postgres role per service, matching `overview.md` §3 as closely as table-level grants allow:

| Postgres role | Tables |
|---|---|
| `app_ingest` | `INSERT` on `items` · `SELECT` on `items`, `conversations`, `suggestions` (routing check, `data-model.md` §2.5) |
| `app_resolver` | `SELECT, UPDATE` on `items` · `SELECT, INSERT, UPDATE` on `conversations` · `SELECT, INSERT` on `item_embeddings` |
| `app_committer` | `SELECT, UPDATE` on `items` · `INSERT` on `obligations`, `latents` · `INSERT` on `dead_letters` (§2.1) |
| `app_dispatcher` | `SELECT` on `items`, `obligations` · `UPDATE` on `obligations` (`reminder_sent_at`) · `SELECT, INSERT` on `capacity_snapshots` · `SELECT, INSERT, UPDATE` on `suggestions` · `SELECT, UPDATE` on `latents` |

`sa-extractor` has no Postgres role because it has no Cloud SQL binding at all (§2.1) — there's nothing to grant.

---

## 3. Vertex AI / ADK configuration

`sa-extractor` and `sa-resolver` are the only service accounts with `roles/aiplatform.user`. Environment variables common to both:

```
GCP_PROJECT_ID=<project>
VERTEX_LOCATION=us-central1
GEMINI_MODEL=<confirm exact Vertex Model Garden resource name at build time>
```

The exact Gemini 3.5 Flash model resource string is deliberately left as a placeholder — Vertex AI model identifiers are confirmed against the Model Garden at the time of building, not guessed here.

---

## 4. OAuth, Calendar, Gmail

- `google-oauth-client-secret` (Secret Manager) holds the OAuth client ID/secret used for the onboarding flow (PRD §10).
- Each user's refresh token is its own secret, `user-refresh-token-{user_id}`; `users.google_refresh_token_ref` (`data-model.md` §2) stores that secret's full resource name (`projects/{project}/secrets/user-refresh-token-{id}/versions/latest`), not the token itself.
- `sa-committer` and `sa-dispatcher` are granted `secretAccessor` at the **project level** for Secret Manager, not per-secret. Simplification, stated plainly: at hackathon scale (one demo user, a handful at most) per-secret IAM conditions add real complexity for no practical isolation benefit — every user's token is equally sensitive to the same two services either way. If this became a real multi-tenant product, scope this to per-secret conditions; noted as a deliberate scale-appropriate call, same spirit as ADR 0004.
- **Calendar API quota:** closes the open item from `capacity-engine.md` §8. Default Calendar API quota (per-user, per-100-second buckets) comfortably covers two reads per user per dispatcher run (the 14-day trailing window + 7-day forward window) at any hackathon-relevant user count. No quota increase request needed.

---

## 5. Cloud Scheduler and manual trigger

Two jobs, both HTTP targets against `dispatcher-svc`'s Cloud Run URL, authenticated via OIDC token (`roles/run.invoker` granted to the Scheduler job's service account on `dispatcher-svc`):

```
dispatch-daily   : cron "0 7 * * *"   → POST /dispatch
dispatch-midday  : cron "0 13 * * *"  → POST /dispatch
```

Both crons run in the single demo user's timezone. **Known single-tenant simplification:** a real multi-user version needs either per-user scheduled jobs or a UTC sweep that checks each user's local 7am — out of scope for a 9-day, effectively single-user build, and not worth solving speculatively.

**Manual trigger for demo/judging** (PRD §12.8, `docs/engineering/conventions.md` repo layout — `scripts/dispatch-now.sh`):
```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$DISPATCHER_URL/dispatch"
```
Same endpoint Cloud Scheduler hits; the developer's own `gcloud` identity satisfies `run.invoker` on `dispatcher-svc` if granted (or the demo simply runs it via `gcloud run services proxy dispatcher-svc` to avoid a separate IAM grant for the presenter). This is the mechanism that lets the capacity engine be shown live on camera without waiting for 7am.

---

## 6. Terraform structure

Per `docs/engineering/conventions.md`'s `infra/` directory. Terraform owns durable, slow-changing infrastructure; application deploys do not go through Terraform (container image tags change every iteration — churning Terraform state on every code change is the wrong tool for that).

```
infra/
  main.tf              # provider, project APIs enabled
  cloud_sql.tf          # instance, database, pgvector extension, app_* Postgres roles
  pubsub.tf              # 3 topics + 3 dlq topics + subscriptions + dead-letter policies
  gcs.tf                  # media bucket + lifecycle rule
  secret_manager.tf      # static secrets (placeholders; per-user secrets created at onboarding time, not by Terraform)
  service_accounts.tf    # 5 service accounts
  iam.tf                  # every binding in §2.1, resource-scoped
  cloud_scheduler.tf      # dispatch-daily, dispatch-midday
  cloud_run.tf             # service *shells* (name, service account, min-instances=0) — revisions/images deployed separately
  variables.tf / outputs.tf
```

Per-user Secret Manager secrets (`user-refresh-token-{user_id}`) are created at onboarding time by `committer-svc`'s own code path (via the Secret Manager API, `roles/secretmanager.admin` scoped narrowly to secret-creation — a small addition to `sa-committer` beyond §2.1's `secretAccessor`), not by Terraform — Terraform provisions the two static secrets and the IAM policy allowing dynamic secret creation, not the per-user secrets themselves.

**Application deploys** (`scripts/deploy.sh`, one `gcloud run deploy` per service, image built via Cloud Build/Artifact Registry) plus `terraform apply` together satisfy the README requirement (PRD §12.4) of "no manual console steps" — two scripted commands, not a `gcloud` sequence for infra *and* deploys mixed together.

---

## 7. Local dev

Restates and makes concrete what `docs/engineering/conventions.md` already named:

- **Pub/Sub emulator:** `gcloud beta emulators pubsub start --project=local-dev`, then `PUBSUB_EMULATOR_HOST=localhost:8085` in every service's `.env.local`. Topics/subscriptions are created against the emulator by a small `scripts/setup-emulator.sh` that mirrors `pubsub.tf`'s topic list — kept as a plain script, not Terraform-against-the-emulator, since the emulator is ephemeral per dev session.
- **Cloud SQL Auth Proxy:** `cloud-sql-proxy <instance-connection-name>` against the real dev Cloud SQL instance (ADR 0004 — there's only one instance, so local dev talks to it directly through the proxy rather than a local Postgres container, avoiding schema drift).
- **Twilio locally:** not emulated. `ingest-svc` is exposed via `ngrok` (or Cloud Run itself, redeployed) when testing the live SMS path; unit/integration tests (`docs/engineering/conventions.md` — capacity engine, state machine, critical-path integration) don't require a real Twilio webhook at all.

---

## 8. Cost control (restates PRD §9, made concrete)

- `min-instances=0` on all five Cloud Run services — Terraform default in `cloud_run.tf`.
- Cloud SQL smallest shared-core tier; stopped after the demo is recorded:
  ```bash
  gcloud sql instances patch <instance> --activation-policy=NEVER
  ```
- Submission does not require a live deployment at judging time (hackathon rules) — only proof one existed, satisfied by Cloud Run logs and dashboard screenshots captured during the demo recording (PRD §13).

---

## 9. Doc set complete

This closes every "→ `infrastructure.md`" pointer from `overview.md`, `data-model.md`, `capacity-engine.md`, and `agent-contracts.md`. The six-doc architecture set (`overview`, `state-machine`, `data-model`, `capacity-engine`, `agent-contracts`, `infrastructure`) is now internally consistent — each resolved gap was traced back and the doc that raised it was updated to point here rather than left stale. Remaining work moves from documentation to implementation: `docs/engineering/conventions.md` governs how that code gets written.
