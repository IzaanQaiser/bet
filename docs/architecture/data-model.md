# Data Model

Third doc in the architecture set — see `overview.md` §0. Canonical schema: where the PRD's §8 sketch and this doc disagree, this doc wins — the PRD is a first pass, this is what actually gets migrated. Deviations are called out explicitly in §3.

Single Cloud SQL for PostgreSQL instance, `pgvector` extension enabled (ADR [0004](../decisions/0004-single-postgres-instance.md)). No ORM — this DDL is what `migrations/0001_init.sql` will contain when service code-writing starts (`docs/engineering/conventions.md`).

---

## 1. Entity relationship diagram

```mermaid
erDiagram
    users ||--o{ items : owns
    users ||--o{ capacity_snapshots : owns
    users ||--o{ conversations : owns
    items ||--o| obligations : "1:1 if type=obligation"
    items ||--o| latents : "1:1 if type=latent"
    items ||--o| item_embeddings : "1:1"
    items ||--o{ suggestions : "surfaced as"
    items ||--o{ conversations : "clarified via"
    items ||--o{ dead_letters : "failed at"
    items }o--o| items : "parent_item_id (thread attach)"
    capacity_snapshots ||--o{ suggestions : "scored against"
```

---

## 2. DDL

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164            text NOT NULL UNIQUE,
    google_refresh_token_ref text,          -- reference into Secret Manager, not the token itself
    timezone              text NOT NULL,     -- IANA tz name, e.g. 'America/Los_Angeles'
    working_hours_start   time NOT NULL DEFAULT '09:00',
    working_hours_end     time NOT NULL DEFAULT '18:00',
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE items (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users(id),
    raw_channel    text NOT NULL DEFAULT 'sms' CHECK (raw_channel IN ('sms')),
    raw_media_uri  text,                     -- GCS URI, null for text-only
    ingested_at    timestamptz NOT NULL DEFAULT now(),

    type           text NOT NULL CHECK (type IN ('obligation', 'latent')),
    state          text NOT NULL CHECK (state IN (
                       'RECEIVED', 'EXTRACTED', 'DUPLICATE_SUSPECTED',
                       'CLARIFYING', 'NEEDS_REVIEW', 'AWAITING_CONFIRMATION',
                       'CANCELLED', 'CONFIRMED', 'COMMITTED', 'MERGED', 'FAILED'
                   )),

    title          text,
    summary        text,
    effort_minutes int CHECK (effort_minutes IN (15, 30, 60, 120, 240)),
    focus_depth    text CHECK (focus_depth IN ('shallow', 'deep')),
    confidence     numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),

    dedupe_hash    text,                     -- sha256(lower(trim(title)) || '|' || lower(trim(summary))); see §2.1
    parent_item_id uuid REFERENCES items(id),

    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_items_user_state ON items(user_id, state);
CREATE INDEX idx_items_dedupe_hash ON items(dedupe_hash) WHERE dedupe_hash IS NOT NULL;

CREATE TABLE obligations (
    item_id            uuid PRIMARY KEY REFERENCES items(id),
    due_at             timestamptz,
    calendar_event_id  text,
    reminder_sent_at   timestamptz,
    reminder_window_hours int NOT NULL DEFAULT 24,
    action_type        text NOT NULL DEFAULT 'calendar' CHECK (action_type IN ('calendar', 'email')),
    email_draft        text,
    email_sent_at      timestamptz
);

CREATE TABLE latents (
    item_id           uuid PRIMARY KEY REFERENCES items(id),
    last_surfaced_at  timestamptz,
    surface_count     int NOT NULL DEFAULT 0,
    dismissal_count   int NOT NULL DEFAULT 0,
    dormant_until     timestamptz          -- reused for both dismissal-dormancy (30d) and snooze (7d); see state-machine.md §2.2
);
CREATE INDEX idx_latents_dormant_until ON latents(dormant_until) WHERE dormant_until IS NOT NULL;

CREATE TABLE item_embeddings (
    item_id    uuid PRIMARY KEY REFERENCES items(id),
    embedding  vector(768) NOT NULL          -- text-embedding-004
);
CREATE INDEX idx_item_embeddings_hnsw ON item_embeddings
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE capacity_snapshots (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  uuid NOT NULL REFERENCES users(id),
    date                     date NOT NULL,
    free_minutes             int NOT NULL,
    largest_contiguous_block int NOT NULL,
    fragmentation_index      numeric(4,3) NOT NULL,
    load_delta               numeric(5,3) NOT NULL,
    computed_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, date)
);

CREATE TABLE suggestions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id      uuid NOT NULL REFERENCES items(id),
    user_id      uuid NOT NULL REFERENCES users(id),   -- denormalized; see §2.2
    snapshot_id  uuid NOT NULL REFERENCES capacity_snapshots(id),
    sent_at      timestamptz NOT NULL DEFAULT now(),
    outcome      text CHECK (outcome IN ('accepted', 'dismissed', 'snoozed', 'no_response')),
    responded_at timestamptz
);
CREATE INDEX idx_suggestions_user_open ON suggestions(user_id) WHERE outcome IS NULL;
CREATE INDEX idx_suggestions_item ON suggestions(item_id);

CREATE TABLE conversations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users(id),
    item_id        uuid NOT NULL REFERENCES items(id),
    exchange_count int NOT NULL DEFAULT 0,
    last_message_at timestamptz NOT NULL DEFAULT now(),
    pending_fields text[],                   -- e.g. {'due_at'}; rendering detail in agent-contracts.md
    resolved_fields jsonb NOT NULL DEFAULT '{}'::jsonb  -- obligation-specific values with no items/obligations column to live in pre-commit; see §2.4
);
CREATE INDEX idx_conversations_user ON conversations(user_id);

CREATE TABLE dead_letters (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id     uuid NOT NULL REFERENCES items(id),
    stage       text NOT NULL,               -- topic name the message failed on, e.g. 'items.extracted'
    payload     jsonb NOT NULL,               -- the message body itself, inline; see §2.3
    error       text NOT NULL,
    retry_count int NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_dead_letters_item ON dead_letters(item_id);
```

### 2.1 `dedupe_hash` — cheap prefilter before the embedding call

Not described in the PRD's resolver algorithm (§5.2), only listed in the schema sketch. Clarified here: on entering `EXTRACTED`, `resolver-svc` computes `dedupe_hash` and checks for an exact match *before* running the embedding search. An exact hash match is treated identically to a `similarity ≥ 0.92` embedding match — routed to `DUPLICATE_SUSPECTED`, same "is this the same as X?" flow, never silently merged (ADR 0003's "never merge silently" applies regardless of which check found the match). The only thing the hash prefilter buys is skipping a Vertex AI embedding call in the common case of an exact or near-exact resend — it does not change the dedupe UX.

### 2.2 `suggestions.user_id` — denormalized, not in the PRD sketch

Added here. The inbound-SMS routing check in `state-machine.md` §4 ("a `suggestions` row for this user with `outcome IS NULL`") runs on **every inbound SMS**, synchronously, before the message can be handled. Without `user_id` on `suggestions`, that query needs a join through `items`; with it, it's `idx_suggestions_user_open`, a partial index scan on `user_id` alone. Worth the denormalization for a query on the hot path; `item_id` is kept too since most other queries (recording an outcome, joining to `capacity_snapshots`) key off it naturally.

### 2.3 `dead_letters.payload` — inline, not a reference

The PRD sketch names this column `payload_ref`, implying a pointer (e.g. to GCS). Resolved here as inline `jsonb`, renamed to `payload`: every message on `items.raw` / `items.extracted` / `items.confirmed` is a small structured JSON envelope (ids, refs, extracted fields — never raw media bytes, which live in GCS and are referenced by URI within the payload itself). There's nothing large enough here to justify a second storage hop; inlining keeps a failed message replayable with one row read instead of a row read plus a GCS fetch.

### 2.4 `conversations.resolved_fields` — where `due_at` lives before commit

**Resolved bug, not in the PRD sketch at all.** `due_at` has no column on `items` — it lives only on `obligations` (§2, above) — and `resolver-svc` has no Postgres grant on `obligations` (`infrastructure.md` §2.2; only `committer-svc` writes it, at commit time). So as originally written, once the clarification call (`agent-contracts.md` §3.2) resolved a `due_at` from a reply, there was nowhere for `resolver-svc` to durably persist it before the user confirms — Cloud Run instances are stateless between invocations, so it can't just be held in memory across turns either.

Fixed here: `resolved_fields` holds obligation-specific values resolved during the pipeline but not yet committed — `due_at` today; `action_type`/`email_draft` if/when the email-action stretch is built (`agent-contracts.md` flags that mechanism as still unspecified). **`resolver-svc` creates the `conversations` row unconditionally the moment it consumes an `items.extracted` message** — not only when clarification is actually needed — and immediately stages any `due_at` the extractor already produced into `resolved_fields`. This means the zero-clarification-needed path (straight to `AWAITING_CONFIRMATION`) still has somewhere for `due_at` to live, not just the multi-exchange path. One `conversations` row per item, used as `resolver-svc`'s scratchpad from `EXTRACTED` through to `CONFIRMED`/`CANCELLED`/`NEEDS_REVIEW`/`MERGED`.

`title`/`summary`/`effort_minutes`/`focus_depth`/`confidence` don't have this problem — they're already columns on `items`, and `resolver-svc` has `UPDATE` on `items` (`infrastructure.md` §2.2), so those get written straight there as they're resolved.

### 2.5 `conversations.state` — removed from the PRD sketch

The PRD sketch lists a `state` column on `conversations`. Dropped here: it would duplicate `items.state`, which already distinguishes `CLARIFYING` from `AWAITING_CONFIRMATION` for the same item, and two columns tracking the same fact is a drift risk (which one does `resolver-svc` trust if they ever disagree?). "Is this conversation open" is answered by joining to `items.state IN ('CLARIFYING', 'AWAITING_CONFIRMATION')` — a two-table join on indexed columns (`conversations.user_id`, `items` primary key), cheap enough at this scale. `items.state` remains the single source of truth for pipeline position, full stop.

---

## 3. Summary of deviations from the PRD §8 sketch

The PRD is intentionally a first-pass sketch, not the canonical schema — this section exists so the gap is visible rather than silent:

| PRD sketch | This doc | Why |
|---|---|---|
| `dead_letters.payload_ref` | `dead_letters.payload jsonb`, inline | §2.3 — nothing here is large enough to warrant a reference |
| `conversations.state` present | Removed | §2.5 — redundant with `items.state`, drift risk |
| `conversations` has no field for resolved-but-uncommitted obligation data (`due_at` etc.) | Added `resolved_fields jsonb` | §2.4 — nowhere else to stage it before commit |
| `suggestions` has no `user_id` | Added, denormalized | §2.2 — hot-path routing query needs it join-free |
| `latents` has no explicit snooze column | `dormant_until` reused for snooze, not a new column | `state-machine.md` §2.2 — one column, two callers, documented here per that doc's own note |

---

## 4. Migration strategy

Per `docs/engineering/conventions.md`: this DDL becomes `migrations/0001_init.sql` verbatim when service code-writing starts, applied via `scripts/migrate.sh`. Forward-only — no down-migrations are written; a mistake becomes a new numbered migration, not a rollback script.

---

## 5. Open items for sibling docs

- ~~Exact rendering of `conversations.pending_fields` into a batched SMS question, and the literal Pub/Sub message shapes that reference these rows~~ → done, see `agent-contracts.md` §1, §3.2.
- `capacity_snapshots` is written by `dispatcher-svc` once per user per day (`UNIQUE (user_id, date)` enforces one row) — the exact computation producing `free_minutes` / `largest_contiguous_block` / `fragmentation_index` / `load_delta` is `capacity-engine.md`'s job, not this doc's.
- ~~Service account roles that map to the write-access matrix in `overview.md` §3 (who gets `INSERT`/`UPDATE` on which table, concretely)~~ → done, see `infrastructure.md` §2.2.
