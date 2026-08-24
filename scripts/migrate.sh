#!/usr/bin/env bash
# Applies every not-yet-applied migrations/*.sql file, in order, against
# DATABASE_URL, tracking what's already run in a schema_migrations table.
# Local dev: point DATABASE_URL at the Cloud SQL Auth Proxy (conventions.md,
# infrastructure.md §7) — e.g.
#   DATABASE_URL="postgresql://waslyrideshare%40gmail.com:x@127.0.0.1:5433/obligation_engine" ./scripts/migrate.sh
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL first — see docs/engineering/conventions.md}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
    "CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"

for f in "$SCRIPT_DIR"/../migrations/*.sql; do
    name="$(basename "$f")"
    already_applied="$(psql "$DATABASE_URL" -tAc "SELECT 1 FROM schema_migrations WHERE filename = '$name';")"
    if [ "$already_applied" = "1" ]; then
        echo "Skipping $name (already applied)."
        continue
    fi
    echo "Applying $name..."
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
        "INSERT INTO schema_migrations (filename) VALUES ('$name');"
done
echo "All migrations applied."
