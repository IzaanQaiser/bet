-- Web division Phase 5 follow-up — DELETE /me/items/{id} lets the user
-- clear an item from their own dashboard (real feedback: deleting the
-- Google Calendar event directly left it stranded in the dashboard,
-- since nothing synced the deletion back). Soft-delete via
-- state='CANCELLED', not a real row DELETE — matches the existing
-- state-machine vocabulary (items.state already allows 'CANCELLED',
-- migrations/0001_init.sql) and keeps the audit trail obligations/
-- conversations/suggestions rows reference intact.

GRANT UPDATE ON items TO "sa-dashboard@obligation-engine-hack.iam";
