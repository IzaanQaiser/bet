-- capacity_snapshots is upserted (INSERT ... ON CONFLICT (user_id, date) DO
-- UPDATE), not just inserted — a repeated /dispatch run on the same day
-- updates that day's snapshot rather than erroring or duplicating. Postgres
-- requires UPDATE privilege for the DO UPDATE clause, not just INSERT.
-- 0001_init.sql only granted SELECT, INSERT (the originally planned set,
-- data-model.md/infrastructure.md §2.2, written before this upsert design
-- decision) — found via a real permission-denied error on the first live
-- dispatcher-svc deploy (step 8).
GRANT UPDATE ON capacity_snapshots TO "sa-dispatcher@obligation-engine-hack.iam";
