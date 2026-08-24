# Build Status

Read this after the PRD at the start of every session — it's the fast answer to "where are we." Update it whenever a build-order step completes, starts, or gets blocked. This is a living tracker, not a history — keep it short and current; git log is the historical record.

**Last updated:** 2026-08-23 (session 3)

---

## Phase

**Implementation started — on the `dev` branch, working on Step 1.**

All six architecture docs (`docs/architecture/`), all ADRs (`docs/decisions/`), engineering conventions (`docs/engineering/conventions.md`), and the PRD (`docs/product/prd.md`) are written, cross-referenced, and internally consistent as of a full cohesiveness pass on 2026-08-22 (found and fixed one real bug: `due_at` had nowhere to be staged pre-commit — see `docs/architecture/data-model.md` §2.4). PRD §14's build order was subsequently split from 12 steps into 19 smaller, single-concern ones, each with a minimal "Reads" doc list and a checkable "Done when" signal.

## Current step — PRD §14 build order

**Steps 1-4 done. Next: Step 5 — `resolver-svc` stub (auto-confirm).**

| Step | Status |
|---|---|
| **Phase A — Foundation** | |
| 1. Infra skeleton (Terraform) | **Done** — applied to `obligation-engine-hack`, all acceptance criteria verified (idempotent, IAM scoping confirmed, resource inventory confirmed) |
| 2. DB schema + shared package | **Done** — migration applied, all 11 tests pass (8 unit + 3 integration, run for real against live Cloud SQL) |
| 3. `ingest-svc` + real Twilio number | **Done** — deployed to Cloud Run, real Twilio number (`+14152365420`) wired to it, a real SMS from the developer's phone was confirmed end-to-end in the database (`state='RECEIVED'`, correct `user_id`). Two real deploy-time bugs found and fixed — see Notes. |
| **Phase B — Core pipeline (auto-confirm stub)** | |
| 4. `extractor-svc` | **Done** — deployed to Cloud Run, real end-to-end verified: a real `RawItemMessage` published to `items-raw` produced a correct `ExtractedItemMessage` on `items-extracted` via the real Gemini 3.5 Flash call, `type="obligation"`, `due_at` correctly left null for ambiguous "Friday", `effort_minutes=15` (int). Three real deploy-time bugs found and fixed — see Notes. |
| 5. `resolver-svc` stub (temporary, auto-confirm) | Not started |
| 6. `committer-svc` | Not started |
| **Phase C — The differentiator** | |
| 7. Capacity engine, pure functions | Not started |
| 8. `dispatcher-svc` | Not started |
| **Phase D — Trust and quality features** | |
| 9. Real `resolver-svc` — confirmation | Not started |
| 10. Real `resolver-svc` — clarification loop | Not started |
| 11. Multimodal ingest | Not started |
| 12. Dedupe via embeddings | Not started |
| **Phase E — Resilience and polish** | |
| 13. DLQ + error handling | Not started |
| 14. Feedback loop / dismissal scoring | Not started |
| 15. Email draft + send action (stretch) | Not started |
| **Phase F — Ship** | |
| 16. Seed demo data script | Not started |
| 17. Record demo | Not started |
| 18. README, diagram export, write-up | Not started |
| 19. Bonus (blog, social, Veo, Lyria, real onboarding) | Not started |

## Blockers

None.

## Decided

- Account: `waslyrideshare@gmail.com`. Project: `obligation-engine-hack` (plain `obligation-engine` was already taken globally).
- Billing account `01153A-78309A-856476` linked and enabled. (History: was closed due to a declined card; user paid the balance and reopened it; hit a billing-account project-quota limit next, resolved by unlinking `msa-gpt` to free a slot.)
- `obligation-engine-db` (Cloud SQL Postgres 15, `db-f1-micro`) is live at connection name `obligation-engine-hack:us-central1:obligation-engine-db`.
- Media bucket: `obligation-engine-hack-media`. Artifact Registry: `us-central1-docker.pkg.dev/obligation-engine-hack/obligation-engine`.
- Twilio: Account SID `AC3292d4a7944b87b2fe3db562856e32bd`; Auth Token in Secret Manager (`twilio-auth-token`); an API Key's secret also captured and stored (`twilio-api-key-secret`) for outbound sends in later steps — see `infrastructure.md` §4.1 for why there are two separate Twilio credentials, not one.
- Twilio account was **upgraded to paid** (not trial) — a $20 balance was added, and full A2P 10DLC brand + campaign registration was completed (Brand "Izaan Qaiser", Sole Proprietor). Real owned number: **+14152365420** (415/SF area code, Local, SMS+MMS+Voice, $1.15/mo), webhook configured to `https://ingest-svc-ns4t52sm7a-uc.a.run.app/webhook/sms`. Public compliance docs (privacy policy, terms, opt-in page — required for A2P campaign approval) are hosted at https://gist.github.com/IzaanQaiser/ee4ef5c5e3f1320287358b021f8b920f (a public gist, deliberately separate from the private project repo).
- Demo user bootstrapped in `users` table: `+16477401694`, `timezone='America/Toronto'`, default working hours.

## Notes for the next session

- Local toolchain: `uv`, `ruff`, `psql` (via `libpq`), `cloud-sql-proxy` all installed (`$HOME/.local/bin` and `/opt/homebrew/opt/libpq/bin` added to PATH in `~/.zshrc`).
- `migrations/0001_init.sql` applied to the real `obligation_engine` database (schema + table-level GRANTs for the four service IAM users). `shared/obligation_engine_shared` (schemas, db, pubsub helpers) built and tested — `uv run pytest shared/tests/` passes 11/11 when a Cloud SQL Auth Proxy is running (`DB_USER`, `DB_HOST`, `DB_PORT`, `GCP_PROJECT_ID`, `CLOUD_SQL_INSTANCE` env vars — see `shared/tests/test_migration.py` docstring); the 3 integration tests skip cleanly without it.
- Real finding worth knowing: Cloud SQL's `cloudsqlsuperuser` role does **not** include `CREATEDB` — scratch-database create/drop for tests goes through `gcloud sql databases`, not raw SQL. Documented in `infrastructure.md` §2.2 and the test file itself.
- Migration/admin access: run as the developer's own IAM identity (`waslyrideshare@gmail.com`, granted `cloudsqlsuperuser`) through the proxy — never as any of the four service accounts. See `infrastructure.md` §2.2's "Migration/admin bootstrap" note.
- `services/ingest-svc` exists: FastAPI app, real Twilio `RequestValidator`-based signature check, lookup-or-reject on `users.phone_e164` (onboarding is deferred — see below), publishes to `items-raw`. Dockerfile builds and runs correctly — two real multi-stage-build gotchas fixed along the way: (1) the `uvicorn` console script's shebang hardcodes the builder stage's venv path, fixed by invoking `python -m uvicorn` instead; (2) `uv sync`'s default editable install of workspace packages (`shared`) points back to the builder's source tree, which doesn't exist at runtime — fixed with `--no-editable`. Both are worth remembering for every other service's Dockerfile.
- Local Pub/Sub emulator installed (`gcloud components install pubsub-emulator` + `beta`) and OpenJDK (needed to run it) — both via Homebrew, PATH persisted in `~/.zshrc`. `scripts/setup-emulator.sh` creates the 6 topics against it, mirroring `infra/pubsub.tf`.
- `scripts/migrate.sh` rewritten to track applied migrations in a `schema_migrations` table — it used to blindly reapply every file every time, which fails once a table exists. Real bug found via this: `items.type` was `NOT NULL`, impossible for `ingest-svc` to satisfy since type is unknown until `EXTRACTED`. Fixed via `migrations/0002_items_type_nullable.sql`.
- Cloud SQL Auth Proxy + Pub/Sub emulator were left running in the background at the end of this session (ports 5433 and 8085) — may need restarting in a fresh session/terminal.
- **Two real deploy-time bugs found getting `ingest-svc` actually working in Cloud Run, both worth remembering for every later service:**
  1. `roles/cloudsql.client` alone does **not** grant IAM database authentication — the separate `roles/cloudsql.instanceUser` role is what actually authorizes connecting as a specific IAM DB user. Local testing never caught this because it ran as the developer's own Owner-level identity, which bypasses the check. Added to all four service accounts in `infra/cloud_sql.tf`.
  2. Cloud Run's native `--add-cloudsql-instances` Unix socket does **not** transparently inject an IAM token the way the standalone `cloud-sql-proxy --auto-iam-authn` does for local dev — the application itself has to fetch a real OAuth token (scope `sqlservice.admin`) and use it as the password. `shared/obligation_engine_shared/db.py` now branches on this explicitly.
  3. (Smaller) No service had been granted `SELECT` on `users` at all — added to all four via `migrations/0003_grant_users_select.sql`, since every service will need it eventually (timezone, working hours, refresh token ref).
- `scripts/deploy.sh` now exists (per `infrastructure.md` §6) — `./scripts/deploy.sh <service-name>` builds (with `--platform linux/amd64`, required on Apple Silicon), pushes, and deploys. `/healthz` had to be renamed to `/health` — `/healthz` specifically collided with something at the Google Frontend layer (returned a generic Google 404 page instead of reaching the container at all); the real functional route (`/webhook/sms`) was never affected.
- Every step now has full acceptance criteria and named unit/integration/manual tests in `docs/engineering/test-plan.md` — read that step's section before starting it, and don't consider a step done until its tests pass, not just its code.
- Onboarding (PRD §10) is deliberately not in the critical path — bootstrap the single demo user's OAuth token and `users` row manually (see PRD §14's scope note) rather than building the real SMS onboarding flow. That flow only happens in step 19, if time allows.
- Demo needs seeded/backdated data (`docs/product/prd.md` §13, "Demo data note" + step 16) — don't leave this until step 17.
- CI (GitHub Actions) deliberately not set up yet — add once step 4–6 produces real code and tests to run against.
- **Step 4 (`extractor-svc`) findings, verified empirically in a scratch venv before writing production code (`/tmp/adk-probe`), both documented in `agent-contracts.md` §2 and `infrastructure.md` §2/§3:**
  1. Vertex AI's structured-output schema only supports **string** enum values — `Literal[15, 30, ...]` (ints) fails schema validation outright. The wire-facing Pydantic model (`_ExtractionResult` in `main.py`) uses `Literal["15", "30", "60", "120", "240"]`; cast to `int` when building the real `ExtractedItemMessage`.
  2. `gemini-3.5-flash` 404s on every regional Vertex AI endpoint tried (`us-central1`, `us-east5`, `us-east1`, `europe-west4`) — it's only served via the **global** endpoint. `VERTEX_LOCATION`/`GOOGLE_CLOUD_LOCATION` must be `global`, confirmed with a real successful `generateContent` call.
  3. ADK's `Event.output` attribute is `None` even with `output_schema` set on the `LlmAgent` — the actual structured JSON text is in `event.content.parts[-1].text` and has to be parsed/validated manually. `main.py`'s `_extract()` does this.
  - Code written: `services/extractor-svc/{main.py,pyproject.toml,Dockerfile}` + 7 passing unit tests (envelope decode, error paths, string→int cast, ADK event parsing — all mocked, no live Gemini calls in CI). `scripts/deploy.sh` extended with an `extractor-svc` case: `--no-allow-unauthenticated`, no `--add-cloudsql-instances` (zero DB access, ADR 0003), and it now also grants the Pub/Sub push service agent `roles/run.invoker` on the service and creates/updates its `items-raw` push subscription (with DLQ policy + the publisher/subscriber IAM the DLQ policy needs) — all imperative, matching `pubsub.tf`'s existing "not here" comment.
  - **Doc-hygiene fix, found while wiring this up:** `infrastructure.md` §6 and `infra/main.tf`'s header comment referenced a `cloud_run.tf` that was never actually created — Cloud Run services, their `run.invoker` bindings, and their push subscriptions are all created imperatively in `scripts/deploy.sh`, not Terraform (they need each other in sequence: service → URL → subscription). Docs corrected to match reality; no code behavior changed.
  - **Real deploy-time bugs found and fixed, both worth remembering for `resolver-svc`/`committer-svc` (steps 5–6, same push-subscription pattern):**
    1. Pub/Sub's dead-letter policy has a real minimum of `max_delivery_attempts=5` — the planned `3` was rejected outright by the API. Fixed in `scripts/deploy.sh` and both places in `docs/` that assumed `3` (`infrastructure.md` §1, `test-plan.md` step 13).
    2. The project's Pub/Sub push service agent (`service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com`) didn't exist yet — enabling `pubsub.googleapis.com` doesn't auto-create it. One-time fix: `gcloud beta services identity create --service=pubsub.googleapis.com --project=obligation-engine-hack` (documented in `infrastructure.md` §2.2's bootstrap note, not made Terraform since it needs the `google-beta` provider for one one-time call).
    3. The originally planned "Pub/Sub push service agent only" invoker pattern doesn't actually work that way — the push subscription's OIDC token has to be minted *as the consuming service's own SA* (`sa-extractor`), not as the raw push agent, which needs a `serviceAccountUser`-style grant the developer's Owner account still can't get on a Google-managed agent. Working pattern (now in `scripts/deploy.sh` and `infrastructure.md` §2.1): grant the push agent `roles/iam.serviceAccountTokenCreator` *on* `sa-extractor`, then grant `sa-extractor` `roles/run.invoker` *on* the Cloud Run service.
  - Deployed and verified for real: published a `RawItemMessage` directly to the live `items-raw` topic ("Bro send rent by Friday"), confirmed the correct `ExtractedItemMessage` arrived on `items-extracted` and Cloud Run logs show a clean `200 OK` with no retries.
