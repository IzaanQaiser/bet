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
