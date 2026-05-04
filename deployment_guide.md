# Deployment Guide — Borderless Radiology Backend

This guide explains how to deploy the FastAPI backend on a **new App VM** (the same VM that hosts the frontend) while keeping Postgres on the **existing DB VM** at `216.48.185.8:5433`.

It covers:

1. The two-VM topology and what has to be true for them to talk.
2. Configuring the DB VM to accept remote connections from the App VM.
3. Installing and configuring the backend on the App VM.
4. Running the database bootstrap + migration scripts (one-time and per release).
5. Running the app as a long-lived service (uvicorn / systemd).
6. Verification + troubleshooting.

> Code is not modified by anything in this guide. All cross-VM concerns are handled via `.env`, network/firewall config, and Postgres' own `postgresql.conf` / `pg_hba.conf`.

---

## 1. Topology

```
+--------------------------+              +---------------------------+
|   App VM (new)           |              |   DB VM (existing)        |
|                          |              |                           |
|  - frontend              |              |  - postgres (docker or    |
|  - this backend (FastAPI)|  TCP 5433    |    native), port 5433     |
|    -> uvicorn :8000      | -----------> |  - db: borderless         |
|  - alembic / scripts     |              |  - schema: rad_incubation |
|                          |              |  - user: radar            |
+--------------------------+              +---------------------------+
        public IP                              216.48.185.8
```

Key facts:

- The backend connects to Postgres over TCP using the URL in `.env`:
  `postgresql+asyncpg://radar:<DB_PASSWORD_URLENCODED>@216.48.185.8:5433/borderless`
  (URL-encode any special chars in the password — e.g. `@` becomes `%40` — do not store the raw form here.)
- The backend itself listens on port `8000` by default. The frontend on the same VM can hit `http://localhost:8000` or `http://127.0.0.1:8000`.
- All schema/table creation happens **from the App VM** via `alembic upgrade head`. The DB VM only hosts Postgres — it does not need the application code.

---

## 2. Prepare the DB VM to accept remote connections

The DB VM previously was probably reached only from itself (`localhost`). For the App VM to connect, three things must be true on the DB VM:

### 2.1 Postgres must listen on a non-loopback address

Edit the DB's `postgresql.conf` (or the equivalent Docker env / compose file):

```conf
listen_addresses = '*'        # or the DB VM's private IP
port = 5433
```

If Postgres runs in Docker on the DB VM, this is usually already true (the container binds `0.0.0.0:5433` on the host). Confirm with:

```bash
# on the DB VM
sudo ss -ltnp | grep 5433
# expect: 0.0.0.0:5433  (good)  or  127.0.0.1:5433  (will NOT work cross-VM)
```

If you see `127.0.0.1:5433`, the container's port mapping is `127.0.0.1:5433:5432`. Change it to `5433:5432` (bind on all interfaces) and restart the container.

### 2.2 `pg_hba.conf` must allow the App VM

Add a host entry permitting the App VM's IP for the `radar` user / `borderless` database. Find `pg_hba.conf` (often `/var/lib/postgresql/data/pg_hba.conf` inside the container) and append:

```conf
# TYPE  DATABASE     USER    ADDRESS              METHOD
host    borderless   radar   <APP_VM_IP>/32       scram-sha-256
```

Replace `<APP_VM_IP>` with the App VM's public or private IP (private if both VMs are on the same VPC/subnet — preferred). Then reload Postgres:

```bash
# native install
sudo systemctl reload postgresql

# docker
docker exec <postgres_container> pg_ctl reload -D /var/lib/postgresql/data
```

### 2.3 The DB VM's firewall / security group must open 5433 to the App VM

This is the most common cause of "connection refused" / "timeout".

- **Cloud security group / VPC firewall:** add an inbound rule on the DB VM allowing TCP `5433` from the App VM's IP (or the VPC CIDR). Do **not** open `5433` to `0.0.0.0/0`.
- **Host firewall (ufw / firewalld):**
  ```bash
  # ufw
  sudo ufw allow from <APP_VM_IP> to any port 5433 proto tcp

  # firewalld
  sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=<APP_VM_IP> port port=5433 protocol=tcp accept"
  sudo firewall-cmd --reload
  ```

### 2.4 Quick sanity check from the App VM

Before installing anything, verify the App VM can actually reach the DB VM:

```bash
# on the App VM
nc -vz 216.48.185.8 5433
# expect: "Connection to 216.48.185.8 5433 port [tcp/*] succeeded!"

# and a real auth check (install postgres-client first if needed):
PGPASSWORD='<DB_PASSWORD>' psql -h 216.48.185.8 -p 5433 -U radar -d borderless -c "SELECT version();"
```

If both succeed, the network/auth path is good and the rest of this guide is just installing the app.

---

## 3. Install the backend on the App VM

### 3.1 System prerequisites

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git postgresql-client
```

(Use whatever package manager matches the VM. The two requirements are **Python ≥ 3.11** and the `psql` client for sanity-checks and running the SQL bootstrap.)

### 3.2 Get the code onto the App VM

```bash
# pick a stable location
sudo mkdir -p /opt/borderless
sudo chown $USER:$USER /opt/borderless
cd /opt/borderless

git clone <your-repo-url> backend
cd backend
git checkout Develop   # or whichever branch you ship from
```

### 3.3 Create a virtualenv and install dependencies

```bash
cd /opt/borderless/backend
python3.11 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -e .              # installs from pyproject.toml
# or, if you maintain a requirements file:
# pip install -r requirements.txt
```

### 3.4 Create `.env` on the App VM

`.env` lives at the project root and is read by `app/config.py` (pydantic-settings). It must point at the DB VM, not localhost:

> **Do not commit real secrets to this file.** Pull the actual values from your secrets store (1Password / Vault / team-shared `.env`). The block below is a template — replace every `<...>` placeholder.

```env
# Database — points at the DB VM. URL-encode any special chars in the password (e.g. '@' -> %40).
DATABASE_URL=postgresql+asyncpg://radar:<DB_PASSWORD_URLENCODED>@216.48.185.8:5433/borderless

# App
API_URL=http://localhost:8000
API_AUTH_KEY='<API_AUTH_KEY>'
5C_API_AUTH_KEY='<5C_API_AUTH_KEY>'

# LLM
GEMINI_API_KEY="<GEMINI_API_KEY>"
GEMINI_MODEL=gemini-2.5-flash-lite

# Slack (ops alerts)
SLACK_WEBHOOK_URL=<SLACK_WEBHOOK_URL>

# 7-day sweep cron
SEVEN_DAY_JOB_ENABLED=true
SEVEN_DAY_JOB_INTERVAL_MINUTES=60

# External callback (decision API target)
EXTERNAL_CALLBACK_URL=https://e2e-qa-api.5cnetwork.com
EXTERNAL_CALLBACK_KEY='<EXTERNAL_CALLBACK_KEY>'
```

Lock down permissions:

```bash
chmod 600 /opt/borderless/backend/.env
```

> The only field that has to differ from your local `.env` is the **host part of `DATABASE_URL`** (it must resolve to the DB VM, not `localhost`). Everything else is identical.

---

## 4. Run database bootstrap + migrations from the App VM

The DB VM hosts Postgres but knows nothing about the app schema. The App VM is responsible for both initial bootstrap and ongoing Alembic migrations.

### 4.1 One-time: create the database + schema (only if not already done)

If the `borderless` database and `rad_incubation` schema already exist on the DB VM (which they should, since you've been running locally against it), skip this step.

If you need to bootstrap from scratch, run **one** of the following from the App VM:

**Option A — apply the SQL file remotely:**
```bash
PGPASSWORD='<DB_PASSWORD>' psql \
  -h 216.48.185.8 -p 5433 -U radar -d postgres \
  -f scripts/vm-init-borderless.sql
```

**Option B — use the bash bootstrap (requires the connecting user to be a superuser, usually `postgres`):**
```bash
PGHOST=216.48.185.8 PGPORT=5433 PGUSER=postgres PGPASSWORD='<superuser-pw>' \
APP_DB_NAME=borderless APP_DB_USER=radar \
./scripts/init_db.sh
```

Both are idempotent — re-running them is safe.

### 4.2 Apply Alembic migrations

This is the step you'll repeat on every deploy that includes new migrations:

```bash
cd /opt/borderless/backend
source .venv/bin/activate
alembic upgrade head
```

`alembic/env.py` reads `DATABASE_URL` from `.env`, so as long as `.env` points at the DB VM, this runs the migrations on the remote Postgres. Verify:

```bash
alembic current
# should print the latest revision id (e.g. 20260430_0006_callback_status_skipped)
```

### 4.3 Other one-shot scripts (run only when needed)

These are in `scripts/` and you run them the same way as Alembic — from the App VM, with the venv activated and `.env` present:

| Script | Purpose | When to run |
|---|---|---|
| `scripts/upload_groundtruth_csv.py` | Load the 80-case ground-truth pool | One-time, when seeding the pool |
| `scripts/upload_test_cases_csv.py` | Load test case CSV | As needed |
| `scripts/classify_groundtruth_pathologies.py` | Backfill pathology classifications | After ground-truth load |
| `scripts/case_anonymization.py` | Anonymize DICOMs | Per pipeline trigger |
| `scripts/send_study_to_webhook.py` | Manual webhook send | Debug / replay |
| `scripts/yotta_push.py` | Push to Yotta storage | As needed |

Example:
```bash
cd /opt/borderless/backend
source .venv/bin/activate
python scripts/upload_groundtruth_csv.py path/to/groundtruth.csv
```

---

## 5. Run the FastAPI app as a long-lived service

For dev/smoke testing, you can just run uvicorn in the foreground:

```bash
cd /opt/borderless/backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For real deployment, run it under `systemd` so it restarts on crash and on reboot.

### 5.1 systemd unit

Create `/etc/systemd/system/borderless-backend.service`:

```ini
[Unit]
Description=Borderless Radiology Backend (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/opt/borderless/backend
EnvironmentFile=/opt/borderless/backend/.env
ExecStart=/opt/borderless/backend/.venv/bin/uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 \
    --workers 2 --proxy-headers
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now borderless-backend
sudo systemctl status borderless-backend
journalctl -u borderless-backend -f
```

### 5.2 Reverse proxy (optional)

If the frontend on the App VM is served by nginx, add a location block:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Otherwise, hitting `http://<app-vm>:8000` directly is fine — just open port 8000 in the App VM's firewall to whoever needs to reach it (the frontend can use `localhost`, so external opening is only needed if n8n / external callers hit the API directly).

### 5.3 Open the App VM's inbound port for external callers

If n8n or anything outside the App VM needs to call the backend:

```bash
# example: allow only n8n's IP
sudo ufw allow from <N8N_IP> to any port 8000 proto tcp
```

---

## 6. Verify end-to-end

Run these on the App VM after starting the service:

```bash
# 1. Health check
curl -s http://localhost:8000/health
# expected: 200 OK with health payload

# 2. Verify DB connectivity from inside the app
#    (the app would have already failed to start if the DB wasn't reachable;
#    journalctl shows the connection error if so)
journalctl -u borderless-backend --since "5 min ago" | grep -i -E "error|database|connect"

# 3. Confirm Alembic state matches code
alembic current
alembic heads
# both should print the same revision id
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `connection refused` from App VM | Postgres bound to `127.0.0.1` only, OR security group blocks 5433 | §2.1, §2.3 |
| `no pg_hba.conf entry for host …` | App VM IP not whitelisted in `pg_hba.conf` | §2.2 |
| `password authentication failed for user "radar"` | Password wrong or `@` not URL-encoded in `DATABASE_URL` | Use `%40` for `@` |
| `relation "..." does not exist` after deploy | Forgot `alembic upgrade head` on the new App VM | §4.2 |
| `ModuleNotFoundError: app` when running `alembic` | Not in project root or venv not active | `cd /opt/borderless/backend && source .venv/bin/activate` |
| App boots but `/health` 502s through nginx | App listening on `127.0.0.1` but nginx on different host | use `--host 0.0.0.0` in uvicorn ExecStart |
| 7-day sweep not running | `SEVEN_DAY_JOB_ENABLED=false` in `.env`, or service crashed and restarted in a loop | check `.env` and `journalctl -u borderless-backend` |

### Useful logs

```bash
# app
journalctl -u borderless-backend -f

# Postgres on the DB VM (docker)
docker logs -f <postgres_container>

# Postgres on the DB VM (native)
sudo journalctl -u postgresql -f
```

---

## 8. Migration / release checklist

Every time you push a new release to the App VM:

1. `git pull` on the App VM under `/opt/borderless/backend`.
2. `source .venv/bin/activate && pip install -e .` (only if dependencies changed).
3. `alembic upgrade head` (only if new migrations were added — safe to run every time, it's a no-op when current).
4. `sudo systemctl restart borderless-backend`.
5. `curl -s http://localhost:8000/health` to confirm it came back up.
6. `journalctl -u borderless-backend -n 100 --no-pager` to confirm no startup errors.

That's the full deployment loop. The DB VM is untouched on every release — only Alembic talks to it, from the App VM.
