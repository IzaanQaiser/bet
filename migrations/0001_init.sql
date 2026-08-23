-- Canonical schema, copied verbatim from docs/architecture/data-model.md §2.
-- If this file and that doc ever disagree, this file is buggy, not the doc.

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

    dedupe_hash    text,                     -- sha256(lower(trim(title)) || '|' || lower(trim(summary))); see data-model.md §2.1
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
    user_id      uuid NOT NULL REFERENCES users(id),   -- denormalized; see data-model.md §2.2
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
    resolved_fields jsonb NOT NULL DEFAULT '{}'::jsonb  -- obligation-specific values with no items/obligations column to live in pre-commit; see data-model.md §2.4
);
CREATE INDEX idx_conversations_user ON conversations(user_id);

CREATE TABLE dead_letters (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id     uuid NOT NULL REFERENCES items(id),
    stage       text NOT NULL,               -- topic name the message failed on, e.g. 'items.extracted'
    payload     jsonb NOT NULL,               -- the message body itself, inline; see data-model.md §2.3
    error       text NOT NULL,
    retry_count int NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_dead_letters_item ON dead_letters(item_id);

-- Table-level GRANTs for the four service IAM database users, per
-- infrastructure.md §2.2. Deferred to this migration rather than Terraform
-- because they need these tables to exist first (infra/cloud_sql.tf's own
-- comment says as much). Note: infrastructure.md §2.2 used illustrative
-- names ("app_ingest" etc.) — the real Postgres roles are the IAM database
-- users Terraform actually created (infra/cloud_sql.tf), named after each
-- service account's email. sa-extractor has no SQL user at all (ADR 0003)
-- and so gets no GRANT here — there's nothing to grant it.

GRANT INSERT ON items TO "sa-ingest@obligation-engine-hack.iam";
GRANT SELECT ON items, conversations, suggestions TO "sa-ingest@obligation-engine-hack.iam";

GRANT SELECT, UPDATE ON items TO "sa-resolver@obligation-engine-hack.iam";
GRANT SELECT, INSERT, UPDATE ON conversations TO "sa-resolver@obligation-engine-hack.iam";
GRANT SELECT, INSERT ON item_embeddings TO "sa-resolver@obligation-engine-hack.iam";

GRANT SELECT, UPDATE ON items TO "sa-committer@obligation-engine-hack.iam";
GRANT INSERT ON obligations, latents, dead_letters TO "sa-committer@obligation-engine-hack.iam";

GRANT SELECT ON items, obligations TO "sa-dispatcher@obligation-engine-hack.iam";
GRANT UPDATE ON obligations TO "sa-dispatcher@obligation-engine-hack.iam";
GRANT SELECT, INSERT ON capacity_snapshots TO "sa-dispatcher@obligation-engine-hack.iam";
GRANT SELECT, INSERT, UPDATE ON suggestions TO "sa-dispatcher@obligation-engine-hack.iam";
GRANT SELECT, UPDATE ON latents TO "sa-dispatcher@obligation-engine-hack.iam";
