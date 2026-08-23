# Architecture Overview

Canonical, implementation-grade architecture for the Capacity-Aware Obligation Engine. Product intent lives in [`docs/product/prd.md`](../product/prd.md) — read that first for *why*. This doc and its siblings in `docs/architecture/` are the *how*, at the level of detail an implementer should not have to guess past.

Sibling docs (written one at a time, in this order):
1. **`overview.md`** (this doc) — service topology, write-access boundaries, data flow
2. `state-machine.md` — item and latent lifecycle, transition rules, failure handling (done)
3. `data-model.md` — schema, indexes, migration strategy (done)
4. `capacity-engine.md` — snapshot computation and scoring, worked examples
5. `agent-contracts.md` — exact I/O schemas and prompts for Extractor, Resolver, Dispatcher
6. `infrastructure.md` — GCP resource inventory, IAM bindings, IaC structure

Until a sibling doc exists, do not invent its content to unblock implementation — flag the gap instead (per `AGENTS.md`).

---

## 1. Design principles this architecture enforces

These are inherited from the PRD's non-negotiables (§15) and translated into structural constraints, not conventions:

- **No orchestrator agent.** Control flow is an explicit state machine (`state-machine.md`). LLM agents decide *content* (what to extract, what to ask, what to suggest); they never decide *what happens next in the pipeline*. This is enforced by construction — agents are invoked by pipeline code with a fixed next-step, not given tools to call the next stage themselves.
- **Write access is scoped per service, enforced by IAM.** Not "the extractor shouldn't write to the calendar" as a convention the code happens to follow — the extractor's service account has no Calendar or Gmail scope and no DB write role. A prompt injection in a photographed note cannot escalate past what the service account can physically do.
- **Confirm-before-write is structural, not a prompt instruction.** Only `committer-svc` holds write credentials to Calendar/Gmail, and it only ever consumes messages from the `items.confirmed` topic, which only `resolver-svc` publishes to, which only happens after an explicit user affirmative is parsed. There is no code path from raw ingest to a calendar write that skips confirmation.
- **Every cross-service handoff is durable and replayable.** Pub/Sub between every stage, not direct service-to-service calls. A crashed service doesn't lose the item — it redelivers. This is also what makes the dead-letter queue possible at every stage uniformly.

---

## 2. Service topology

Five Cloud Run services, one Cloud Scheduler job, five Pub/Sub topics (four pipeline + one fan-out for dead-letters per topic in practice, detailed in §4).

```mermaid
flowchart TB
    subgraph ext["External"]
        twilio["Twilio SMS/MMS"]
        gcal["Google Calendar API"]
        gmail["Gmail API"]
        vertex["Vertex AI — Gemini 3.5 Flash + text-embedding-004"]
    end

    subgraph gcp["Google Cloud"]
        ingest["ingest-svc\n(Cloud Run)"]
        extractor["extractor-svc\n(Cloud Run, ADK)"]
        resolver["resolver-svc\n(Cloud Run, ADK)"]
        committer["committer-svc\n(Cloud Run)"]
        dispatcher["dispatcher-svc\n(Cloud Run, ADK)"]
        scheduler["Cloud Scheduler\n(07:00 + midday)"]
        gcs["GCS\n(media, 30d TTL)"]
        sql["Cloud SQL\nPostgres + pgvector"]

        t1["items.raw"]
        t2["items.extracted"]
        t3["items.confirmed"]
    end

    twilio -->|webhook| ingest
    ingest -->|store media| gcs
    ingest -->|publish| t1
    t1 --> extractor
    extractor -->|Gemini call, no write scope| vertex
    extractor -->|publish| t2
    t2 --> resolver
    resolver -->|embed| vertex
    resolver -->|read/write items, CLARIFYING/CONFIRMED| sql
    resolver -->|SMS clarify/confirm| twilio
    resolver -->|publish on confirm| t3
    t3 --> committer
    committer -->|write event| gcal
    committer -->|send draft, stretch| gmail
    committer -->|write COMMITTED| sql

    scheduler --> dispatcher
    dispatcher -->|read next 7d| gcal
    dispatcher -->|read/write snapshots, suggestions| sql
    dispatcher -->|reminder + suggestion SMS| twilio
    dispatcher -->|publish on suggestion accept| t3
```

### Service responsibilities

| Service | Responsibility | Invoked by |
|---|---|---|
| `ingest-svc` | Validate Twilio webhook signature, persist media to GCS, publish raw item to `items.raw`. No LLM calls. | Twilio webhook (sync HTTP) |
| `extractor-svc` | Consume `items.raw`, call Gemini 3.5 Flash with strict schema, publish to `items.extracted`. | Pub/Sub push |
| `resolver-svc` | Consume `items.extracted`, run dedupe + clarification + confirmation over SMS, hold `conversations` state, publish to `items.confirmed`. | Pub/Sub push + Twilio inbound SMS webhook (a conversation spans multiple inbound messages) |
| `committer-svc` | Consume `items.confirmed`, write to Calendar (and Gmail, stretch), mark item `COMMITTED`. | Pub/Sub push |
| `dispatcher-svc` | Compute capacity snapshots, fire deadline reminders, score latents, send at most one suggestion. | Cloud Scheduler (cron) + manual trigger endpoint for demo/judging |

`ingest-svc` is the single Twilio-facing webhook — Twilio supports one messaging webhook per number, so every inbound SMS, whether a brand-new item or a reply mid-conversation, lands there first. `ingest-svc` does a cheap routing check (open `conversations` row for this user → forward to `resolver-svc`; else a pending, unanswered `suggestions` row → forward to `dispatcher-svc`; else treat as a new item and publish to `items.raw`) via a synchronous internal call authenticated by Cloud Run service-to-service IAM, not a public route. Full routing precedence and reply semantics are in `docs/architecture/state-machine.md`. This is the one asymmetry in an otherwise uniform "topic in, topic out" topology — `ingest-svc` sometimes calls a downstream service directly instead of only publishing.

---

## 3. Write-access matrix

This table is the security story. Enforced via per-service service accounts and IAM roles — not application-level checks.

| Service | Reads | Writes | Vertex AI access | Google OAuth scopes |
|---|---|---|---|---|
| `ingest-svc` | Twilio payload | GCS (media), `items.raw` topic | none | none |
| `extractor-svc` | `items.raw` | `items.extracted` topic | Gemini (generate) | **none** |
| `resolver-svc` | `items.extracted`, `items` (own conversation), `item_embeddings` | `items` (state `CLARIFYING`/`CONFIRMED`), `conversations`, `item_embeddings`, `items.confirmed` topic, Twilio (outbound SMS) | Gemini (generate), embeddings | **none** |
| `committer-svc` | `items.confirmed` | `items` (state `COMMITTED`), `obligations` | none | Calendar (write), Gmail (send, stretch only) |
| `dispatcher-svc` | `items`, `latents`, `capacity_snapshots`, `suggestions` | `capacity_snapshots`, `suggestions`, `latents` (surface metadata only), Twilio (outbound SMS), `items.confirmed` topic (publish, and only after parsing an explicit accept reply to a suggestion — same confirm-before-write rule as `resolver-svc`, see ADR 0003) | none | Calendar (read only) |

Read this table top to bottom and confirm: **no service that touches untrusted user input (`ingest-svc`, `extractor-svc`) holds any external write credential.** The only services with Calendar/Gmail write scope (`committer-svc`) or Calendar read scope (`dispatcher-svc`) never see raw user input directly — they only consume already-confirmed, already-structured state.

---

## 4. Data flow: durability and failure handling

Every arrow in §2 that crosses a service boundary via Pub/Sub follows the same pattern:

```mermaid
sequenceDiagram
    participant P as Publisher
    participant T as Topic
    participant S as Subscriber
    participant D as Dead-letter topic

    P->>T: publish(message)
    T->>S: push delivery (attempt 1..3)
    alt ack within deadline
        S-->>T: ack
    else nack or timeout, 3x
        T->>D: forward to <topic>.dlq
        D->>S: dead_letters row written\n(item_id, stage, payload_ref, error)
    end
```

Concretely: `items.raw`, `items.extracted`, `items.confirmed` each have a paired `.dlq` topic. A subscriber that fails 3 delivery attempts (application error, not a 4xx from bad input — those nack immediately with a reason logged) lands the message in the DLQ, and a row is written to `dead_letters` with enough context (`item_id`, `stage`, `payload_ref`, `error`) to replay manually. This is what "queryable state" in the PRD's state machine section actually rests on — every item's current state and failure history is reconstructable from `items` + `dead_letters` alone, no log spelunking required.

`dispatcher-svc` has no upstream topic (it's cron-triggered) so it has no DLQ in this sense; its failure mode is "the run didn't happen" and is handled by Cloud Scheduler retry + alerting, not a dead-letter row.

---

## 5. Correlation and observability

Every service logs structured JSON with `item_id` (or, for dispatcher runs with no single item, `dispatch_run_id`) as a correlation field. This is what lets a judge — or you, on camera — follow one screenshot from Twilio webhook through extraction, clarification, confirmation, and calendar write in Cloud Logging, filtered on a single ID. No distributed tracing system is being introduced for a 9-day build; structured logs with a consistent correlation key is the entire observability strategy, and it's sufficient for this scale.

---

## 6. Open items for sibling docs

Flagging rather than deciding here, per `AGENTS.md`:

- Exact Pub/Sub message schemas per topic → `agent-contracts.md`
- ~~`conversations` state machine detail (how clarification exchanges are counted, batched, and exhausted)~~ → done, see `state-machine.md`
- Capacity snapshot computation detail and worked numeric examples → `capacity-engine.md`
- Terraform/gcloud resource list, service account names, exact IAM role bindings → `infrastructure.md`
- Local dev story (Pub/Sub emulator, Cloud SQL proxy) → `infrastructure.md`
