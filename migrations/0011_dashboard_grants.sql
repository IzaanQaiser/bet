-- Web division Phase 5 — dashboard-svc is read-only across the whole
-- pipeline's data (items/obligations/suggestions/conversations/messages),
-- scoped to the caller's own rows in application code (every /me/* query
-- is WHERE user_id = <from the session JWT>), not by Postgres itself —
-- same "GCP IAM is a hard boundary, in-table row scoping is a software
-- invariant" split infrastructure.md §2 already describes for every
-- other service. SELECT only: dashboard-svc never writes anything except
-- the caller's own users row (timezone/working hours), covered below.

GRANT SELECT ON items, conversations, messages, obligations, suggestions
    TO "sa-dashboard@obligation-engine-hack.iam";

-- Also UPDATE on users, not just SELECT: PATCH /me/profile
-- (timezone/working hours) writes the caller's own row.
GRANT SELECT, UPDATE ON users TO "sa-dashboard@obligation-engine-hack.iam";
