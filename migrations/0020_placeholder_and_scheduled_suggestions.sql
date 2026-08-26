-- Real Calendar event id for this latent's currently-scheduled [idea]-tagged
-- placeholder (null: no fitting slot, no linked Google account, or currently
-- dormant). Written by dispatcher-svc (already has UPDATE on latents), read
-- by committer-svc (already SELECT on latents, migration 0005) to decide
-- whether accept should PATCH this event in place or POST a new one.
ALTER TABLE latents ADD COLUMN placeholder_event_id text;

-- Suggestions created at exact next_fit_start-trigger time have no
-- capacity_snapshots row to attach to (the batch-scoring sweep this FK
-- assumed is gone — see ADR 0009). scheduled_for carries the date/time this
-- suggestion was actually made for, replacing capacity_snapshots.date for
-- that purpose.
ALTER TABLE suggestions ALTER COLUMN snapshot_id DROP NOT NULL;
ALTER TABLE suggestions ADD COLUMN scheduled_for timestamptz;

-- ADR 0009 — committer-svc now clears a promoted item's placeholder
-- columns itself (accept path), needing UPDATE on latents for the first
-- time (it previously only ever INSERTed a fresh row, migration 0005).
GRANT UPDATE ON latents TO "sa-committer@obligation-engine-hack.iam";
