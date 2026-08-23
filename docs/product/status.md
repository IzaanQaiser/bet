# Build Status

Read this after the PRD at the start of every session — it's the fast answer to "where are we." Update it whenever a build-order step completes, starts, or gets blocked. This is a living tracker, not a history — keep it short and current; git log is the historical record.

**Last updated:** 2026-08-22 (session 2)

---

## Phase

**Implementation started — on the `dev` branch, working on Step 1.**

All six architecture docs (`docs/architecture/`), all ADRs (`docs/decisions/`), engineering conventions (`docs/engineering/conventions.md`), and the PRD (`docs/product/prd.md`) are written, cross-referenced, and internally consistent as of a full cohesiveness pass on 2026-08-22 (found and fixed one real bug: `due_at` had nowhere to be staged pre-commit — see `docs/architecture/data-model.md` §2.4). PRD §14's build order was subsequently split from 12 steps into 19 smaller, single-concern ones, each with a minimal "Reads" doc list and a checkable "Done when" signal.

## Current step — PRD §14 build order

**In progress: Step 1 — Infra skeleton.** `infra/*.tf` written (project APIs, 5 service accounts, 6 Pub/Sub topics + publish-side IAM, Cloud SQL instance + IAM DB auth users, GCS bucket, Artifact Registry, Secret Manager secret containers). Not yet applied — blocked, see Blockers below.

| Step | Status |
|---|---|
| **Phase A — Foundation** | |
| 1. Infra skeleton (Terraform) | In progress — code written, not applied |
| 2. DB schema + shared package | Not started |
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

- **Billing account closed.** `gcloud billing accounts list` shows the only account ("My Billing Account", `01153A-78309A-856476`) as `OPEN: False`. Cloud SQL and other paid resources can't be provisioned until this is fixed — reactivated or a new one created — and its ID confirmed. Blocks `terraform apply` for step 1.
- **No GCP project ID chosen yet.** User confirmed a new dedicated project (not reusing `ock2-dev` or others) — proposed ID `obligation-engine`, not yet created or confirmed.

## Notes for the next session

- `infra/` now exists with step 1's Terraform. Once billing + project ID are sorted: `terraform init`, `terraform plan -var="project_id=<id>"`, `terraform apply -var="project_id=<id>"`.
- `services/`, `shared/`, `migrations/` per `docs/engineering/conventions.md` still don't exist — those start at step 2.
- Every step now has full acceptance criteria and named unit/integration/manual tests in `docs/engineering/test-plan.md` — read that step's section before starting it, and don't consider a step done until its tests pass, not just its code.
- Onboarding (PRD §10) is deliberately not in the critical path — bootstrap the single demo user's OAuth token and `users` row manually (see PRD §14's scope note) rather than building the real SMS onboarding flow. That flow only happens in step 19, if time allows.
- Demo needs seeded/backdated data (`docs/product/prd.md` §13, "Demo data note" + step 16) — don't leave this until step 17.
- CI (GitHub Actions) deliberately not set up yet — add once step 4–6 produces real code and tests to run against.
