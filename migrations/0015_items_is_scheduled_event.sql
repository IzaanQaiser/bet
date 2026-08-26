-- Distinguishes a task with a completion deadline (an assignment "due at
-- X" — reminders must fire strictly before X, never at it) from a
-- scheduled event you attend at a specific time (a meeting/party/call
-- "at X" — the reminder that matters most is the one AT X). Decided
-- once at extraction, like type/focus_depth; defaults false so every
-- existing item and every ambiguous future one keeps today's
-- already-correct task behavior.

ALTER TABLE items ADD COLUMN is_scheduled_event boolean NOT NULL DEFAULT false;
