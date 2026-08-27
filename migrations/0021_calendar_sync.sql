-- Two-way Calendar sync (calendar-sync-svc) — reconciles deletions/time
-- changes made directly on Google Calendar back into this system, the
-- reverse of the direction that already worked (dashboard delete ->
-- real Calendar delete). One channel per linked user; channel_token is
-- a random secret echoed back by Google on every push notification,
-- verifying the call actually corresponds to a channel this service
-- registered. sync_token drives the incremental events.list delta.
CREATE TABLE calendar_sync_channels (
    user_id            uuid PRIMARY KEY REFERENCES users(id),
    channel_id         text NOT NULL,
    resource_id        text NOT NULL,
    channel_token      text NOT NULL,
    sync_token         text,
    channel_expiration timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_calendar_sync_channels_channel_id ON calendar_sync_channels(channel_id);

GRANT SELECT, INSERT, UPDATE ON calendar_sync_channels TO "sa-calendar-sync@obligation-engine-hack.iam";
GRANT SELECT ON items, obligations, latents, users TO "sa-calendar-sync@obligation-engine-hack.iam";
GRANT UPDATE ON items, obligations, latents TO "sa-calendar-sync@obligation-engine-hack.iam";

-- Real bug, found in the same pass: dashboard-svc's DELETE /me/items/{id}
-- never actually deleted an idea's real placeholder event (only checked
-- obligations.calendar_event_id) — fixed in code to also clear
-- latents.next_fit_start/placeholder_event_id, which needs UPDATE on
-- latents; sa-dashboard only ever had SELECT (migration 0017) before now.
GRANT UPDATE ON latents TO "sa-dashboard@obligation-engine-hack.iam";
