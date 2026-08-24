"""Integration tests against the real dev Cloud SQL instance, via the Auth
Proxy — there's only one instance total (ADR 0004), no local Postgres
container. test_migration_applies_cleanly uses a scratch database created
within that same instance so it can verify a genuinely fresh apply without
touching the real obligation_engine database; the Postgres roles referenced
by the GRANT statements are cluster-wide, not per-database, so the same
migration file applies unmodified against the scratch database too.

Scratch database create/drop goes through `gcloud sql databases` (the Cloud
SQL Admin API), not a raw SQL CREATE DATABASE — found empirically that Cloud
SQL's `cloudsqlsuperuser` role deliberately excludes CREATEDB (Google
restricts this since they manage the underlying instance), so this has to be
a project-IAM-level operation instead of a Postgres-role privilege.

Requires the Cloud SQL Auth Proxy running locally with --auto-iam-authn,
DB_USER set to a superuser IAM identity, and GCP_PROJECT_ID +
CLOUD_SQL_INSTANCE set for the gcloud calls (docs/architecture/
infrastructure.md §2.2's "Migration/admin bootstrap" note). Skipped
automatically if that's not configured, so the rest of the suite still runs
without live infra.
"""

import os
import subprocess
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    "DB_USER" not in os.environ,
    reason="requires a live Cloud SQL Auth Proxy connection — see module docstring",
)

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"
MIGRATION_FILES = sorted(MIGRATIONS_DIR.glob("*.sql"))  # applied in order, per migrate.sh

EXPECTED_TABLES = {
    "users",
    "items",
    "obligations",
    "latents",
    "item_embeddings",
    "capacity_snapshots",
    "suggestions",
    "conversations",
    "dead_letters",
}


def _admin_connection(dbname: str) -> psycopg.Connection:
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "5433"))
    conn = psycopg.connect(
        dbname=dbname, user=os.environ["DB_USER"], password="x", host=host, port=port
    )
    conn.autocommit = True
    return conn


def _gcloud_sql_databases(*args: str) -> None:
    subprocess.run(
        [
            "gcloud",
            "sql",
            "databases",
            *args,
            "--instance",
            os.environ["CLOUD_SQL_INSTANCE"],
            "--project",
            os.environ["GCP_PROJECT_ID"],
            "--quiet",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def scratch_db():
    name = "test_migration_scratch"
    subprocess.run(
        [
            "gcloud",
            "sql",
            "databases",
            "delete",
            name,
            "--instance",
            os.environ["CLOUD_SQL_INSTANCE"],
            "--project",
            os.environ["GCP_PROJECT_ID"],
            "--quiet",
        ],
        capture_output=True,  # ignore failure — db may not exist yet
    )
    _gcloud_sql_databases("create", name)
    yield name
    _gcloud_sql_databases("delete", name)


def test_migration_applies_cleanly(scratch_db):
    conn = _admin_connection(scratch_db)
    for f in MIGRATION_FILES:
        conn.execute(f.read_text())
    conn.close()


def test_all_tables_exist():
    conn = _admin_connection("obligation_engine")
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
    ).fetchall()
    conn.close()
    assert EXPECTED_TABLES.issubset({r[0] for r in rows})


def test_pgvector_extension_enabled():
    conn = _admin_connection("obligation_engine")
    rows = conn.execute("SELECT extname FROM pg_extension WHERE extname = 'vector';").fetchall()
    conn.close()
    assert rows, "pgvector extension is not enabled on obligation_engine"
