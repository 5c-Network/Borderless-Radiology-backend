-- Pure-SQL alternative to init_db.sh. Connect as a superuser first, then run
-- this file against the `postgres` database:
--
--   psql -h <host> -p <port> -U postgres -d postgres -f scripts/init_db.sql
--
-- Idempotent. Safe to re-run.

-- Step 1: create the database if it doesn't exist.
-- (CREATE DATABASE cannot run inside a transaction block, so we use a DO block
--  that conditionally runs it via dblink or checks first. Simplest: check and
--  run from the shell.)
SELECT 'CREATE DATABASE "borderless"'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'borderless'
)\gexec

-- Step 2: connect to the borderless DB and set up extensions + schema.
\c borderless

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS "rad_incubation";

-- (Migrations are applied via `alembic upgrade head` — do NOT create tables here.)
