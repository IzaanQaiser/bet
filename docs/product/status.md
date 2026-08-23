# Build Status

Read this after the PRD at the start of every session — it's the fast answer to "where are we." Update it whenever a build-order step completes, starts, or gets blocked. This is a living tracker, not a history — keep it short and current; git log is the historical record.

**Last updated:** 2026-08-22 (session 2)

---

## Phase

**Implementation started — on the `dev` branch, working on Step 1.**

All six architecture docs (`docs/architecture/`), all ADRs (`docs/decisions/`), engineering conventions (`docs/engineering/conventions.md`), and the PRD (`docs/product/prd.md`) are written, cross-referenced, and internally consistent as of a full cohesiveness pass on 2026-08-22 (found and fixed one real bug: `due_at` had nowhere to be staged pre-commit — see `docs/architecture/data-model.md` §2.4). PRD §14's build order was subsequently split from 12 steps into 19 smaller, single-concern ones, each with a minimal "Reads" doc list and a checkable "Done when" signal.

## Current step — PRD §14 build order

**Steps 1-2 done. Next: Step 3 — `ingest-svc` + real Twilio number.**

| Step | Status |
|---|---|
| **Phase A — Foundation** | |
| 1. Infra skeleton (Terraform) | **Done** — applied to `obligation-engine-hack`, all acceptance criteria verified (idempotent, IAM scoping confirmed, resource inventory confirmed) |
| 2. DB schema + shared package | **Done** — migration applied, all 11 tests pass (8 unit + 3 integration, run for real against live Cloud SQL) |
| 3. `ingest-svc` + real Twilio number | Not started |
| **Phase B — Core pipeline (auto-confirm stub)** | |
| 4. `extractor-svc` | Not started |
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

## Notes for the next session

- Local toolchain: `uv`, `ruff`, `psql` (via `libpq`), `cloud-sql-proxy` all installed (`$HOME/.local/bin` and `/opt/homebrew/opt/libpq/bin` added to PATH in `~/.zshrc`).
- `migrations/0001_init.sql` applied to the real `obligation_engine` database (schema + table-level GRANTs for the four service IAM users). `shared/obligation_engine_shared` (schemas, db, pubsub helpers) built and tested — `uv run pytest shared/tests/` passes 11/11 when a Cloud SQL Auth Proxy is running (`DB_USER`, `DB_HOST`, `DB_PORT`, `GCP_PROJECT_ID`, `CLOUD_SQL_INSTANCE` env vars — see `shared/tests/test_migration.py` docstring); the 3 integration tests skip cleanly without it.
- Real finding worth knowing: Cloud SQL's `cloudsqlsuperuser` role does **not** include `CREATEDB` — scratch-database create/drop for tests goes through `gcloud sql databases`, not raw SQL. Documented in `infrastructure.md` §2.2 and the test file itself.
- Migration/admin access: run as the developer's own IAM identity (`waslyrideshare@gmail.com`, granted `cloudsqlsuperuser`) through the proxy — never as any of the four service accounts. See `infrastructure.md` §2.2's "Migration/admin bootstrap" note.
- `services/` per `docs/engineering/conventions.md` still doesn't exist — starts at step 3.
- Every step now has full acceptance criteria and named unit/integration/manual tests in `docs/engineering/test-plan.md` — read that step's section before starting it, and don't consider a step done until its tests pass, not just its code.
- Onboarding (PRD §10) is deliberately not in the critical path — bootstrap the single demo user's OAuth token and `users` row manually (see PRD §14's scope note) rather than building the real SMS onboarding flow. That flow only happens in step 19, if time allows.
- Demo needs seeded/backdated data (`docs/product/prd.md` §13, "Demo data note" + step 16) — don't leave this until step 17.
- CI (GitHub Actions) deliberately not set up yet — add once step 4–6 produces real code and tests to run against.
