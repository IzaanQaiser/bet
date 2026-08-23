#!/usr/bin/env bash
# Applies every migrations/*.sql file, in order, against DATABASE_URL.
# Local dev: point DATABASE_URL at the Cloud SQL Auth Proxy (conventions.md,
# infrastructure.md §7) — e.g.
#   DATABASE_URL="postgresql://waslyrideshare%40gmail.com:x@127.0.0.1:5433/obligation_engine" ./scripts/migrate.sh
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL first — see docs/engineering/conventions.md}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in "$SCRIPT_DIR"/../migrations/*.sql; do
    echo "Applying $f..."
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
done
echo "All migrations applied."
