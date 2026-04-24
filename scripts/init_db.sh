#!/usr/bin/env bash
# One-shot bootstrap for the borderless database.
#
# - Creates the "borderless" database (if missing).
# - Creates the "rad_incubation" schema (if missing).
# - Ensures the pgcrypto extension for gen_random_uuid().
# - Grants usage to the configured role.
#
# It is safe to re-run. All steps are idempotent.
#
# Expected env vars (loaded from .env or exported beforehand):
#   PGHOST, PGPORT, PGUSER, PGPASSWORD  — connect as a SUPERUSER (usually postgres)
#   APP_DB_NAME                          — defaults to "borderless"
#   APP_DB_USER                          — defaults to PGUSER
#
# Usage:
#   ./scripts/init_db.sh
#
# Then apply migrations:
#   alembic upgrade head

set -euo pipefail

: "${PGHOST:=localhost}"
: "${PGPORT:=5432}"
: "${PGUSER:=postgres}"
: "${APP_DB_NAME:=borderless}"
: "${APP_DB_USER:=$PGUSER}"

export PGPASSWORD="${PGPASSWORD:-}"

psql_admin() {
    psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

psql_db() {
    psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$APP_DB_NAME" -v ON_ERROR_STOP=1 "$@"
}

echo ">> Checking connection..."
psql_admin -c "SELECT version();" >/dev/null

echo ">> Ensuring database '${APP_DB_NAME}'..."
DB_EXISTS=$(psql_admin -tAc "SELECT 1 FROM pg_database WHERE datname = '${APP_DB_NAME}';" || true)
if [ "$DB_EXISTS" != "1" ]; then
    psql_admin -c "CREATE DATABASE \"${APP_DB_NAME}\";"
    echo "   database created."
else
    echo "   database already exists — skipping create."
fi

echo ">> Ensuring pgcrypto extension..."
psql_db -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"

echo ">> Ensuring \"rad_incubation\" schema..."
psql_db -c "CREATE SCHEMA IF NOT EXISTS \"rad_incubation\";"

if [ "$APP_DB_USER" != "$PGUSER" ]; then
    echo ">> Granting privileges to '${APP_DB_USER}'..."
    psql_db -c "GRANT USAGE, CREATE ON SCHEMA \"rad_incubation\" TO \"${APP_DB_USER}\";"
    psql_db -c "GRANT ALL PRIVILEGES ON DATABASE \"${APP_DB_NAME}\" TO \"${APP_DB_USER}\";"
    psql_db -c "ALTER DEFAULT PRIVILEGES IN SCHEMA \"rad_incubation\" GRANT ALL ON TABLES TO \"${APP_DB_USER}\";"
    psql_db -c "ALTER DEFAULT PRIVILEGES IN SCHEMA \"rad_incubation\" GRANT ALL ON SEQUENCES TO \"${APP_DB_USER}\";"
fi

echo ""
echo "Done. Next: apply migrations with:"
echo "  DATABASE_URL=postgresql+asyncpg://${APP_DB_USER}:<password>@${PGHOST}:${PGPORT}/${APP_DB_NAME} alembic upgrade head"
