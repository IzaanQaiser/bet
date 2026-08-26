-- User-directed: the 15/30/60/120/240 bucket system was designed for a
-- task's fuzzy work-time guess (never shown to the user directly). A
-- scheduled event's duration is different — the user is asked for it
-- directly and states an exact number ("1.5 hours"), which then becomes
-- a real Google Calendar event's end time they can see. Rounding that to
-- the nearest bucket (90 min -> 120 min) is a real, visible bug, not an
-- acceptable approximation. Replaced with a sane sanity range instead of
-- a fixed enum: still guards against a nonsensical value (0, negative, or
-- absurdly long), without forcing every duration into one of 5 buckets.
ALTER TABLE items DROP CONSTRAINT items_effort_minutes_check;
ALTER TABLE items ADD CONSTRAINT items_effort_minutes_check
    CHECK (effort_minutes IS NULL OR (effort_minutes > 0 AND effort_minutes <= 1440));
