-- Create the borderless database + rad_incubation schema inside an EXISTING
-- Postgres instance. Safe to re-run.
--
-- Two ways to apply this:
--
-- 1) Against the running postgres container on the VM (one-shot, now):
--    docker exec -i postgres psql -U radar -d postgres \
--        < scripts/vm-init-borderless.sql
--
-- 2) Future-proof: drop this file into the VM's ./init-scripts/ folder (the
--    directory that postgres mounts at /docker-entrypoint-initdb.d).
--    It will run automatically on a fresh volume.
--    Name it with a suffix so it runs AFTER anything already there, e.g.
--    02-create-borderless.sql

-- Step 1: create the database if it doesn't exist.
SELECT 'CREATE DATABASE borderless'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'borderless'
)\gexec

-- Step 2: switch to it and set up the extension + schema.
\c borderless

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS rad_incubation;

-- (Tables are created by Alembic: `alembic upgrade head` on the app side.)
