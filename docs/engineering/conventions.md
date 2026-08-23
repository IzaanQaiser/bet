# Engineering Conventions

How the code gets written, once code-writing starts. Applies to all five services (`docs/architecture/overview.md` §2). See ADR [0006](../decisions/0006-python-runtime.md) for why Python.

## Language & tooling
- **Python 3.12.**
- **`uv`** for dependency management and virtualenvs — single lockfile, fast, no separate `pip`/`venv`/`poetry` juggling mid-hackathon.
- **`ruff`** for both linting and formatting (replaces black + flake8 + isort). One tool, one config, run in a pre-commit-style check before any commit.
- **`pytest`** for tests.
- **No ORM.** Raw SQL via `psycopg` (v3), parameterized queries. Matches AGENTS.md's "simple, explicit, testable designs over speculative abstraction" — the schema in `docs/architecture/data-model.md` is small and stable enough that an ORM buys nothing but indirection for a 9-day build.
- **Google ADK** for the three LLM-driving services (`extractor-svc`, `resolver-svc`, `dispatcher-svc`). `ingest-svc` and `committer-svc` never call an LLM and have no ADK dependency.

## Repo layout
```
services/
  ingest-svc/
  extractor-svc/
  resolver-svc/
  committer-svc/
  dispatcher-svc/
    src/
    tests/
    Dockerfile
    pyproject.toml
shared/                  # installable local package: db access, pubsub helpers, schemas
  obligation_engine_shared/
    db.py
    pubsub.py
    schemas.py           # Pydantic models for Pub/Sub message payloads — single source of truth for the topic contracts in docs/architecture/agent-contracts.md
migrations/
  0001_init.sql
  0002_....sql
infra/                   # Terraform / gcloud scripts — see docs/architecture/infrastructure.md
scripts/
  dispatch-now.sh        # manually trigger dispatcher-svc, for local dev and for judges (PRD §12.8)
docs/
```

Each service is a `uv` workspace member with its own `pyproject.toml`; `shared` is a workspace member every service depends on locally (path dependency), so a Pub/Sub schema change is a one-file edit, not five.

## Migrations
Plain numbered `.sql` files in `migrations/`, applied in order by a small script (`scripts/migrate.sh` — wraps `psql` against `DATABASE_URL`). No migration framework. Every migration is forward-only; a bad migration during a 9-day build gets fixed by a new migration, not a down-migration.

## Message contracts
Every Pub/Sub topic's payload is a Pydantic model in `shared/obligation_engine_shared/schemas.py`. Publishers and subscribers both import the same model — the contract can't drift between services because there's only one definition. This file, not a doc, is the source of truth for exact field names; `docs/architecture/agent-contracts.md` describes intent and points here for the literal schema.

## Testing
Given the timeline, test investment is targeted, not exhaustive:
- **Unit tests, required:** the capacity engine's scoring functions (`fit_score`, `revival_score` — pure functions, easy to get wrong, easy to verify by hand). The state machine's transition function. Dedupe threshold logic.
- **Integration tests, required for the critical path only:** ingest → extract → confirm → commit, against the Pub/Sub emulator and a local Postgres.
- **Not building:** end-to-end tests against real Twilio/Calendar/Gmail. Verified manually during the live demo instead — that's what the demo *is*.

Per-step acceptance criteria and the exact named tests behind each of these bullets live in `docs/engineering/test-plan.md`, one section per PRD §14 build-order step — read only the step you're working.

## Local dev
- **Pub/Sub emulator** (`gcloud beta emulators pubsub start`) — every service reads `PUBSUB_EMULATOR_HOST` when set and talks to the emulator instead of real Pub/Sub.
- **Cloud SQL Auth Proxy** for local Postgres access against the real dev instance (not a local Postgres container) — avoids a schema-drift class of bugs between local and deployed, acceptable given there's only one instance total (ADR [0004](../decisions/0004-single-postgres-instance.md)).
- **`.env.local`** per service for local-only config (gitignored). Never used in deployed environments — see Secrets below.

## Secrets & environment variables
- Deployed services read secrets exclusively via **Secret Manager** (Twilio auth token, Google OAuth client secret/refresh tokens) using Application Default Credentials — never an env var holding a raw secret in Cloud Run config.
- Non-secret config (topic names, project ID, timezone defaults) via plain Cloud Run environment variables.
- Naming: `SCREAMING_SNAKE_CASE`, prefixed by concern where ambiguous (`TWILIO_*`, `GCP_*`, `DB_*`). Exact table lives in the README once services exist (PRD §12.6).

## Logging
Structured JSON to stdout (Cloud Run/Cloud Logging picks this up natively — no logging agent to configure). Every log line includes `item_id` or `dispatch_run_id` per `docs/architecture/overview.md` §5. No print debugging left in committed code.

## Docker
One `Dockerfile` per service, multi-stage: `uv sync --frozen` in a builder stage, copy the resulting venv into a slim runtime stage. No shared base image beyond the public `python:3.12-slim` tag — keeps each service independently buildable and avoids a shared-image versioning problem mid-hackathon.
