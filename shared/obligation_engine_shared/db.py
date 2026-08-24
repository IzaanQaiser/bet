"""Cloud SQL connection helper. No ORM — raw SQL via psycopg (v3), per
docs/engineering/conventions.md.

The two environments authenticate differently — this was a real bug found
during step 3's first live deploy, worth being explicit about:

  - Local dev: TCP, via a manually-run `cloud-sql-proxy ... --auto-iam-authn`
    (docs/architecture/infrastructure.md §7). That flag makes the *proxy*
    transparently substitute a real IAM token for whatever password string
    the client sends — the app never sees or manages a token, "x" works.
  - Deployed (Cloud Run): Unix socket, via `--add-cloudsql-instances`. This
    does **not** do the same transparent substitution — the socket is a
    plain tunnel, and the application itself has to fetch a real IAM access
    token (scope `sqlservice.admin`) and pass that as the password. Using
    "x" here fails with "Cloud SQL IAM service account authentication
    failed", which is exactly what happened until this was fixed.
"""

import os
from typing import Literal
from uuid import UUID, uuid4

import google.auth
import google.auth.transport.requests
import psycopg

_SQL_ADMIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.admin"


def _iam_token() -> str:
    creds, _ = google.auth.default(scopes=[_SQL_ADMIN_SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def get_connection() -> psycopg.Connection:
    user = os.environ["DB_USER"]  # e.g. sa-ingest@<project>.iam — see module docstring
    dbname = os.environ.get("DB_NAME", "obligation_engine")
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

    if instance_connection_name:
        return psycopg.connect(
            dbname=dbname,
            user=user,
            password=_iam_token(),
            host=f"/cloudsql/{instance_connection_name}",
        )

    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "5433"))
    return psycopg.connect(dbname=dbname, user=user, password="x", host=host, port=port)


def log_message(conn: psycopg.Connection, user_id: UUID, direction: Literal["in", "out"], body: str) -> None:
    """Appends one row to the messages table (migrations/0007_messages_table.sql)
    — the durable, both-directions log Phase G's tone-mirroring reads from
    (agent-contracts.md §3). Reused verbatim by ingest-svc (inbound),
    resolver-svc and dispatcher-svc (outbound) — one implementation, not
    three copies that could drift. Does not commit; caller's existing
    transaction boundary owns that, same convention as every other write in
    this codebase."""
    conn.execute(
        "INSERT INTO messages (user_id, direction, body) VALUES (%s, %s, %s)",
        (str(user_id), direction, body),
    )


def create_raw_item(conn: psycopg.Connection, user_id: UUID, text: str) -> UUID:
    """Inserts a new text-only RECEIVED items row — the same shape
    ingest-svc's fresh-message path writes for a brand-new inbound SMS.
    Used by resolver-svc when a reply arrives while an item is open but
    the reply's content doesn't actually relate to it (agent-contracts.md
    §3.5's relates_to_item escape hatch): rather than force the text into
    the open item, resolver-svc gives it its own independent item, via
    this helper plus an items-raw publish, exactly as if it had arrived
    with no open item at all. Does not commit or publish — same
    convention as log_message, caller owns both."""
    item_id = uuid4()
    conn.execute(
        """
        INSERT INTO items (id, user_id, raw_channel, ingested_at, state)
        VALUES (%s, %s, 'sms', now(), 'RECEIVED')
        """,
        (str(item_id), str(user_id)),
    )
    return item_id
