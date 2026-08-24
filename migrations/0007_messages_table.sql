-- Phase G step C: a durable, both-directions message log. Source of "recent
-- message history" for tone-mirroring (agent-contracts.md §3, Phase G step
-- D) — the unified conversational turn feeds a user's own last N messages
-- back to Gemini as a style reference so replies sound like them, not a
-- generic persona. Bundled with its own grants in one file, matching
-- migration 0001's own pattern for brand-new tables (grants split into
-- their own files only for adding access to an *existing* table, as in
-- 0003-0005).

CREATE TABLE messages (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(id),
    direction  text NOT NULL CHECK (direction IN ('in', 'out')),
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_user_created ON messages(user_id, created_at DESC);

-- sa-ingest logs every inbound text (write-only — it never needs to read
-- history back, resolver-svc/dispatcher-svc do that).
GRANT INSERT ON messages TO "sa-ingest@obligation-engine-hack.iam";

-- sa-resolver reads history for tone context and logs its own outbound
-- sends (confirmation cards, clarifying questions, chat replies, etc.).
GRANT SELECT, INSERT ON messages TO "sa-resolver@obligation-engine-hack.iam";

-- sa-dispatcher already sends SMS today (reminders, suggestions) — granted
-- both now rather than deferred to the Phase 2 follow-up (dispatcher's own
-- conversational rewrite), since it's a zero-cost addition to this same
-- migration and Phase 2 will need read access anyway.
GRANT SELECT, INSERT ON messages TO "sa-dispatcher@obligation-engine-hack.iam";

-- sa-extractor: no grant — zero DB access, unchanged (ADR 0003).
-- sa-committer: no grant — confirmed no Twilio/SMS usage anywhere in
-- committer-svc, so it never has an outbound message to log.
