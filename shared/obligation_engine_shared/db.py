"""Cloud SQL connection helper. No ORM — raw SQL via psycopg (v3), per
docs/engineering/conventions.md.

Every environment connects through the Cloud SQL Auth Proxy, which handles
IAM authentication transparently either way — application code never
generates or manages an OAuth token itself, and the password value below is
never actually checked, only required to be a non-empty string by psycopg.

  - Local dev: TCP, via a manually-run `cloud-sql-proxy ... --auto-iam-authn`
    (docs/architecture/infrastructure.md §7).
  - Deployed (Cloud Run): Unix socket, via `--add-cloudsql-instances` — the
    proxy runs automatically, no separate process to manage.
"""

import os

import psycopg


def get_connection() -> psycopg.Connection:
    user = os.environ["DB_USER"]  # e.g. sa-ingest@<project>.iam — see module docstring
    dbname = os.environ.get("DB_NAME", "obligation_engine")
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

    if instance_connection_name:
        return psycopg.connect(
            dbname=dbname,
            user=user,
            password="x",
            host=f"/cloudsql/{instance_connection_name}",
        )

    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "5433"))
    return psycopg.connect(dbname=dbname, user=user, password="x", host=host, port=port)
