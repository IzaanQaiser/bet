-- Replaces the single fixed-window reminder (reminder_window_hours,
-- default 24h, fired once) with two independently-scheduled reminders
-- computed from due_at/effort_minutes (due_at - 2*effort, due_at -
-- effort). Nothing outside dispatcher-svc's _send_reminders reads or
-- writes the two dropped columns (confirmed via full-repo grep).

ALTER TABLE obligations
  DROP COLUMN reminder_window_hours,
  DROP COLUMN reminder_sent_at,
  ADD COLUMN reminder_1_at timestamptz,
  ADD COLUMN reminder_1_sent_at timestamptz,
  ADD COLUMN reminder_2_at timestamptz,
  ADD COLUMN reminder_2_sent_at timestamptz;
