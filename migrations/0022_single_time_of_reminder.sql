-- V1 simplification, user-directed: collapse the two SMS reminders
-- (due_at - 30min, due_at) down to one, at the time-of (due_at itself).
-- The 30-minute lead moves to the Calendar event's own native popup
-- reminder instead (committer_svc/main.py's _write_calendar_event /
-- _create_placeholder_event), not the SMS pipeline. Clean rename rather
-- than a deprecation shim, same convention migration 0013 used when it
-- replaced the original single-reminder columns.

-- Postgres doesn't allow RENAME COLUMN combined with other actions in the
-- same ALTER TABLE statement, hence three separate statements here.
ALTER TABLE obligations
  DROP COLUMN reminder_1_at,
  DROP COLUMN reminder_1_sent_at;

ALTER TABLE obligations RENAME COLUMN reminder_2_at TO reminder_at;
ALTER TABLE obligations RENAME COLUMN reminder_2_sent_at TO reminder_sent_at;
