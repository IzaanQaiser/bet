-- User-directed: a declined suggestion (first decline only, before the
-- 30d-dormancy threshold) now asks the user how long to put it off,
-- rather than silently auto-rescheduling to the next fitting slot. The
-- suggestion stays "open" (outcome still NULL, so ingest-svc keeps
-- routing the user's next reply here) while awaiting that answer.
ALTER TABLE suggestions
  ADD COLUMN awaiting_deferral_reply boolean NOT NULL DEFAULT false;
