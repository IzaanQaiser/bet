-- Found in step 3's real deploy: sa-ingest needs SELECT on users for the
-- phone_e164 lookup in the webhook handler, and this table was never
-- granted to any service in migrations/0001_init.sql or infrastructure.md
-- §2.2's original matrix. Every service will eventually need to read users
-- for something (timezone, working hours, refresh token reference), so
-- granting SELECT to all four now rather than patching this piecemeal.

GRANT SELECT ON users TO "sa-ingest@obligation-engine-hack.iam";
GRANT SELECT ON users TO "sa-resolver@obligation-engine-hack.iam";
GRANT SELECT ON users TO "sa-committer@obligation-engine-hack.iam";
GRANT SELECT ON users TO "sa-dispatcher@obligation-engine-hack.iam";
