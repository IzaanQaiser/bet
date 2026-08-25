"""Web division Phase 3 — the only privileged step before registration
completion (Phase 4). Nothing about joining the waitlist (`registration-svc`'s
`POST /waitlist/join`) required a real phone or any verification; this
script is the human-in-the-loop gate that turns one specific waitlist row
into a real invite. Mirrors bootstrap_oauth_token.py's pattern: direct DB
access via the developer's own IAM Cloud SQL user
(service_accounts.tf's google_sql_user.developer_admin — no new grant
needed), not a service endpoint.

Sets approved_at/approved_by, mints a short-lived signed registration
token (web-session-signing-key, used by registration-svc's Phase 4
endpoints, and by Phase 5's dashboard-svc for its own, separately-scoped
session tokens), and texts the number a /register?token=<token> link — a
plain Twilio REST send via twilio-api-key-secret, same mechanism
resolver-svc/dispatcher-svc already use, not the Verify product (that
happens once the link is clicked, registration-svc's own
POST /register/verify-start). Query param, not a path segment
(/register/<token>) — the frontend is a static export (web/next.config.ts),
which can't pre-render an arbitrary runtime token as a dynamic route.

Usage:
  GCP_PROJECT_ID=obligation-engine-hack DB_USER=<you>@gmail.com \
  DB_HOST=127.0.0.1 DB_PORT=5433 TWILIO_API_KEY_SECRET=... \
  uv run python -u scripts/approve_waitlist.py <phone_e164>

Requires a Cloud SQL Auth Proxy already running (same as any other local
script against the real dev DB — infrastructure.md §7). WEB_BASE_URL
optionally overrides the link's domain (defaults to the GitHub Pages
origin, same fallback scripts/deploy.sh's WEB_ORIGIN uses, until the
plan's manual setup step 1 domain is live).
"""

import os
import sys

from google.cloud import secretmanager
from obligation_engine_shared.tokens import mint_signed_token
from twilio.rest import Client as TwilioClient

# Same identifiers dispatcher-svc/resolver-svc already use (infrastructure.md
# §4.1) — only TWILIO_API_KEY_SECRET is an actual secret, via env.
TWILIO_ACCOUNT_SID = "AC3292d4a7944b87b2fe3db562856e32bd"
TWILIO_API_KEY_SID = "SK7a7912d15fea946956ab8bbae8214bce"
TWILIO_FROM_NUMBER = "+14152365420"

TOKEN_TTL_SECONDS = 24 * 60 * 60
APPROVED_BY = "waslyrideshare@gmail.com"


def _signing_key(project_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/web-session-signing-key/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: approve_waitlist.py <phone_e164>", file=sys.stderr)
        sys.exit(1)
    phone_e164 = sys.argv[1]

    from obligation_engine_shared.db import get_connection

    with get_connection() as conn:
        row = conn.execute(
            "SELECT approved_at FROM waitlist WHERE phone_e164 = %s", (phone_e164,)
        ).fetchone()
        if row is None:
            print(f"{phone_e164} is not on the waitlist.", file=sys.stderr)
            sys.exit(1)
        if row[0] is not None:
            print(f"Note: {phone_e164} already approved at {row[0]}; sending a fresh link anyway.")

    project_id = os.environ["GCP_PROJECT_ID"]
    signing_key = _signing_key(project_id)
    token = mint_signed_token(
        {"phone_e164": phone_e164}, "registration", signing_key, TOKEN_TTL_SECONDS
    )

    base_url = os.environ.get("WEB_BASE_URL", "https://izaanqaiser.github.io")
    link = f"{base_url}/register?token={token}"
    body = f"you're in, tap here to connect your calendar and finish setup: {link}"

    api_key_secret = os.environ["TWILIO_API_KEY_SECRET"]
    twilio_client = TwilioClient(TWILIO_API_KEY_SID, api_key_secret, TWILIO_ACCOUNT_SID)
    twilio_client.messages.create(to=phone_e164, from_=TWILIO_FROM_NUMBER, body=body)
    print(f"Sent registration link to {phone_e164}.")

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE waitlist SET approved_at = now(), approved_by = %s, invite_sent_at = now()
            WHERE phone_e164 = %s
            """,
            (APPROVED_BY, phone_e164),
        )
        conn.commit()
    print(f"waitlist row updated: approved_at, approved_by={APPROVED_BY}, invite_sent_at all set.")


if __name__ == "__main__":
    main()
