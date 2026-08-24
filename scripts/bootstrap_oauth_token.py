"""One-time manual OAuth bootstrap for the single demo user (PRD §14's
scope note — real SMS-driven onboarding is bonus-tier, not built yet).

Runs the OAuth consent flow locally (opens a browser, spins up a
localhost redirect server), mints a refresh token for Calendar + Gmail
send scopes, stores it as a new Secret Manager secret
(user-refresh-token-{user_id}), and updates that user's
google_refresh_token_ref column.

Usage:
  GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
  GCP_PROJECT_ID=obligation-engine-hack DB_USER=<you>@gmail.com \
  DB_HOST=127.0.0.1 DB_PORT=5433 \
  uv run --with google-auth-oauthlib python -u scripts/bootstrap_oauth_token.py <user_id>

Requires a Cloud SQL Auth Proxy already running (same as any other local
script against the real dev DB — infrastructure.md §7).

Run with `python -u` (or `PYTHONUNBUFFERED=1`), not bare `python` — found
the hard way in step 6: `run_local_server()`'s printed auth URL and its
`webbrowser.open()` call both get silently swallowed by Python's default
stdout block-buffering when this runs non-interactively (piped output,
not a real tty). It looks exactly like the script hung with no browser
ever opening; it didn't, the URL was just never actually printed yet.
"""

import os
import sys

from google.cloud import secretmanager
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: bootstrap_oauth_token.py <user_id>", file=sys.stderr)
        sys.exit(1)
    user_id = sys.argv[1]

    client_config = {
        "installed": {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    if not creds.refresh_token:
        print(
            "No refresh_token in the response — this Google account has "
            "already authorized this exact client before. Revoke access at "
            "https://myaccount.google.com/permissions and re-run, or use "
            "prompt='consent' (Google only issues a refresh_token on first "
            "consent, or when explicitly re-prompted).",
            file=sys.stderr,
        )
        sys.exit(1)

    project_id = os.environ["GCP_PROJECT_ID"]
    secret_id = f"user-refresh-token-{user_id}"
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project_id}"

    secret = client.create_secret(
        request={
            "parent": parent,
            "secret_id": secret_id,
            "secret": {"replication": {"automatic": {}}},
        }
    )
    version = client.add_secret_version(
        request={"parent": secret.name, "payload": {"data": creds.refresh_token.encode()}}
    )
    secret_ref = f"{secret.name}/versions/latest"
    print(f"Stored refresh token: {secret_ref}")

    from obligation_engine_shared.db import get_connection

    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET google_refresh_token_ref = %s WHERE id = %s",
            (secret_ref, user_id),
        )
        conn.commit()
    print(f"users.google_refresh_token_ref updated for user_id={user_id}")
    print(f"(secret version resource: {version.name})")


if __name__ == "__main__":
    main()
