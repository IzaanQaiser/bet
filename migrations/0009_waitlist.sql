-- Web division Phase 2 (docs/design plan) — the waitlist itself. Joining is
-- deliberately frictionless: phone number + name, no verification, no
-- OAuth. Nothing privileged happens until a specific number is approved
-- (Phase 3, a developer-run script, not a service endpoint) —
-- approved_at/approved_by/invite_sent_at all stay NULL until then.
-- registration-svc only ever needs SELECT/INSERT here: approval is a
-- direct-DB-access script using the developer's own IAM identity
-- (service_accounts.tf's developer_admin), same pattern as
-- scripts/migrate.sh and bootstrap_oauth_token.py, so the service itself
-- never needs UPDATE on the columns approval touches.

CREATE TABLE waitlist (
    phone_e164     text PRIMARY KEY,
    name           text NOT NULL,
    joined_at      timestamptz NOT NULL DEFAULT now(),
    approved_at    timestamptz,
    approved_by    text,
    invite_sent_at timestamptz
);

GRANT SELECT, INSERT ON waitlist TO "sa-registration@obligation-engine-hack.iam";
