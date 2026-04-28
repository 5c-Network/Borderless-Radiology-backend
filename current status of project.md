# Borderless Radiology Backend — Handoff & Status Document

> **Purpose of this doc:** A single source of truth that explains *what this backend is, how it works, what is done, what is pending, and what is needed to make it live*. Hand this to the next developer and they should be able to take it from "halfway done" to production without further context from anyone else.

**Repository root:** `Borderless radiology backend/` (project root)
**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), PostgreSQL, Alembic, Google Gemini LLM, APScheduler
**Status (as of 2026-04-27):** Backend code complete and runnable in dev. Database is **NOT yet hosted** (intended target is an existing Postgres on a VM). End-to-end live integration with the reporting platform / n8n / external decision system has not been wired up. Pool data has not been ingested. See [§7 Pending / To-Do](#7-pending--to-do-checklist).

---

## Table of Contents
1. [Project Purpose (Business Context)](#1-project-purpose-business-context)
2. [High-Level Architecture & Flow](#2-high-level-architecture--flow)
3. [Application Code — Module Map](#3-application-code--module-map)
4. [API Endpoints](#4-api-endpoints)
5. [Database](#5-database)
6. [Environment Variables](#6-environment-variables)
7. [Pending / To-Do Checklist](#7-pending--to-do-checklist)
8. [How to Run Locally](#8-how-to-run-locally)
9. [How to Deploy / Make it Live](#9-how-to-deploy--make-it-live)
10. [Known Gaps, Risks & Notes](#10-known-gaps-risks--notes)

---

## 1. Project Purpose (Business Context)

Borderless Radiology is a high-trust tier for radiologists. Before a candidate is admitted, they go through a **7-day incubation** in which they read 80 historical cases that already have ground-truth reports. Each submitted report is graded by an LLM against the ground truth. At case 20 (early gate) and at case 80 / 7-day timeout (terminal), the system fires checkpoints, alerts ops on Slack, and POSTs a structured payload to an external decision system.

**This repo is the grading + checkpoint backend.** It does NOT own the radiologist UI or the n8n orchestration — those are the reporting platform and n8n respectively. This service:

- Holds the **pool of 80 ground-truth cases** (`Study_Groundtruth`) and pre-classifies each into `main_pathologies` vs `incidental_findings` using an LLM.
- Serves **activation data** (history + rules + DICOM metadata) so n8n can push the case into the rad's viewer.
- Receives a **submitted candidate report**, calls the LLM grader, persists the result.
- Tracks per-rad progress and **fires checkpoints at case 20, case 80, and at the 7-day timeout** — each with a Slack alert and an external callback POST.

See [`Borderless_Incubation_Workflow.md`](./Borderless_Incubation_Workflow.md) for the product spec.

---

## 2. High-Level Architecture & Flow

### 2.1 System Diagram

```
┌────────────────────────┐       ┌──────────────┐       ┌───────────────────────────┐
│  Reporting Platform    │       │     n8n      │       │  Borderless Radiology     │
│  (radiologist UI)      │──────▶│ orchestrator │──────▶│  Backend (THIS REPO)      │
│                        │ webhk │              │ HTTP  │  FastAPI + Postgres       │
└────────────────────────┘       └──────────────┘       └─────┬─────────────────────┘
        ▲                                                      │
        │                                                      │  - LLM (Gemini)
        │ decision API                                         │  - Slack webhook
        │ (n8n calls platform                                  │  - External callback
        │  with final outcome)                                 │      (decision system)
                                                                ▼
                                                       ┌────────────────────────────┐
                                                       │  PostgreSQL                │
                                                       │  schema: rad_incubation    │
                                                       └────────────────────────────┘
```

### 2.2 End-to-End Flow (per radiologist)

```
                    ┌──────────────────────────────────────────────┐
ONE-TIME SETUP      │ POST /api/v1/study-groundtruth (ingest 80)   │
                    │ POST /api/v1/study-groundtruth/classify      │  ← LLM splits prose
                    │   → fills main_pathologies / incidental list │    pathology blob
                    └──────────────────────────────────────────────┘

                    ┌────────────────────────────────────────────────────┐
RAD STARTS          │ n8n → GET /api/v1/activation-data?rad_id=...       │
                    │       (random pick mode: 2 cases first call,        │
                    │        1 thereafter; assignment row written)        │
                    │                                                     │
                    │ Returns: [{history, rules, dicomData}]              │
                    │                                                     │
                    │ Side effect: rad_state row created if missing,      │
                    │              incubation_started_at stamped.         │
                    └────────────────────────────────────────────────────┘

                    ┌────────────────────────────────────────────────────┐
RAD SUBMITS REPORT  │ n8n → POST /api/v1/grade_case                       │
(repeats per case)  │   body: {rad_id, study_iuid, candidate_report}      │
                    │                                                     │
                    │ 1. enqueue_grading() — verifies rad was assigned    │
                    │    this study_iuid, snapshots GT + candidate, writes│
                    │    a grading_jobs row (status=queued).              │
                    │                                                     │
                    │ 2. Returns 202 with grading_id immediately.         │
                    │                                                     │
                    │ 3. asyncio.create_task(run_grading_job)             │
                    │    a) status=running                                 │
                    │    b) calls Gemini with grading prompt              │
                    │    c) stores grade, score_10pt, critical_miss,      │
                    │       overcall_detected, llm_raw_json,              │
                    │       llm_rationale; status=done                    │
                    │    d) rad_state.cases_completed++                   │
                    │    e) maybe_fire_case_count_checkpoint()            │
                    │       → if count == 20  → gate_20 checkpoint        │
                    │       → if count == 80  → terminal_80 checkpoint    │
                    └────────────────────────────────────────────────────┘

                    ┌────────────────────────────────────────────────────┐
CHECKPOINT FIRES    │ For each kind (gate_20, terminal_80, terminal_7d): │
(20 / 80 / 7 days)  │  - aggregate scores; compute avg, overall_grade,    │
                    │    quality_met, grade_counts, summary               │
                    │  - write checkpoint_events row (UNIQUE on rad_id+kind│
                    │    so each fires exactly once).                     │
                    │  - if gate_20 fails quality → status=suspended_at_20 │
                    │  - if terminal_80          → status=completed_80    │
                    │  - if terminal_7_days      → status=timed_out_7_days│
                    │  - POST Slack webhook                               │
                    │  - POST external callback URL (with X-API-Key)      │
                    └────────────────────────────────────────────────────┘

                    ┌────────────────────────────────────────────────────┐
7-DAY SWEEP         │ APScheduler runs every N min (default 60):          │
(background job)    │  - finds rads in_progress whose incubation_started_at│
                    │    is older than 7 days                              │
                    │  - fires terminal_7_days checkpoint for each         │
                    │    (idempotent via the same uniqueness constraint)   │
                    └────────────────────────────────────────────────────┘
```

### 2.3 Grading Logic (LLM rules — see [`app/prompts.py`](app/prompts.py))

The Gemini grader returns a strict JSON object with a single grade `1 | 2A | 2B | 3A | 3B`:

| Grade | Meaning                                                                | Score (10-pt) |
| :---- | :--------------------------------------------------------------------- | :------------ |
| 1     | All main_pathologies detected, no incidental missed, no overcalls      | 10.0          |
| 2A    | Minor miss/overcall, NOT related to primary indication                 | 8.0           |
| 2B    | Minor miss/overcall, related to primary indication                     | 7.0           |
| 3A    | At least one main pathology missed, NOT related to primary indication  | 5.0           |
| 3B    | At least one main pathology missed AND related to primary indication   | 3.0           |

`score_10pt` is a **fixed lookup** from grade — the LLM's score is overwritten with the canonical value to keep the audit trail clean. `critical_miss = True` iff grade ∈ {3A, 3B}.

Aggregate logic at checkpoints (see [`app/services/grade_utils.py`](app/services/grade_utils.py)):

- avg ≥ 9.0 → overall grade `1`  → **quality_met = True**
- avg ≥ 7.5 → `2A`
- avg ≥ 6.5 → `2B`
- avg ≥ 4.0 → `3A`
- avg < 4.0 → `3B`

Quality bar = overall grade `1`. Anything else means quality not met.

---

## 3. Application Code — Module Map

```
Borderless radiology backend/
├── app/
│   ├── main.py                         # FastAPI entrypoint, mounts routers, schedules 7-day sweep
│   ├── config.py                       # Pydantic settings (loads .env)
│   ├── db.py                           # Async SQLAlchemy engine + session
│   ├── models.py                       # ORM models: StudyGroundtruth, RadState,
│   │                                     CaseAssignment, GradingJob, CheckpointEvent + enums
│   ├── schemas.py                      # Pydantic request/response models + LLM I/O schemas
│   ├── security.py                     # Bare-token Authorization header check
│   ├── prompts.py                      # System + user templates for pool classify and grading
│   ├── api/
│   │   ├── health.py                   # GET /health
│   │   ├── activation.py               # GET /api/v1/activation-data/
│   │   ├── grading.py                  # POST /api/v1/grade_case (+ GET endpoints)
│   │   └── pool.py                     # /api/v1/study-groundtruth (ingest, classify, list)
│   ├── services/
│   │   ├── activation_service.py       # UID-lookup mode + random-pick mode for activation
│   │   ├── grader.py                   # enqueue_grading + run_grading_job (background worker)
│   │   ├── checkpoint.py               # 20/80/7-day aggregate, idempotent fire
│   │   ├── grade_utils.py              # Pure helpers: grade ↔ score, count_grades, quality_met
│   │   ├── llm.py                      # Gemini client wrapper (retries, JSON enforcement)
│   │   ├── slack.py                    # Slack webhook poster + text builder
│   │   ├── external_callback.py        # POST to EXTERNAL_CALLBACK_URL with X-API-Key
│   │   └── summary.py                  # Rule-based 2-line summary for Slack/callback
│   └── jobs/
│       └── seven_day_timeout.py        # APScheduler task: sweep timed-out rads
├── alembic/
│   ├── env.py                          # Async Alembic setup, reads DATABASE_URL
│   └── versions/
│       └── 20260424_0001_initial.py    # Single migration: schema + 5 tables + enums + indexes
├── alembic.ini
├── scripts/
│   ├── init_db.sh                      # Bootstrap script (creates DB, schema, pgcrypto)
│   ├── init_db.sql                     # SQL alternative to init_db.sh
│   └── vm-init-borderless.sql          # ⭐ For the existing VM Postgres (the chosen path)
├── pyproject.toml                      # Deps + tooling
├── .env.example                        # ⭐ Copy → .env and fill in
├── README.md                           # (currently empty — see this doc instead)
└── Borderless_Incubation_Workflow.md   # Product spec
```

---

## 4. API Endpoints

All routes (except `/health`) require `Authorization: <API_AUTH_KEY>` header (bare token; `Bearer <token>` is also accepted). When `API_AUTH_KEY` is empty in the env, auth is **disabled** (dev-only behaviour — do NOT ship to prod with this empty).

### 4.1 Health
- `GET /health` — `{"status":"ok"}`

### 4.2 Pool Management ([`app/api/pool.py`](app/api/pool.py))
- `POST /api/v1/study-groundtruth` — Bulk upsert ground-truth rows. Body: `list[StudyGroundtruthIngest]`. Use this once at setup to seed the 80-case pool.
- `POST /api/v1/study-groundtruth/classify?only_unclassified=true` — Runs LLM on unclassified rows to populate `main_pathologies` / `incidental_findings`.
- `GET /api/v1/study-groundtruth` — List current pool (basic fields).

### 4.3 Activation ([`app/api/activation.py`](app/api/activation.py))
- `GET /api/v1/activation-data/?rad_id=<id>&study_iuids=<csv>` — Two modes:
  - **UID lookup** (`study_iuids` provided): exact lookup, returns matching rows. No assignment tracking.
  - **Random pick** (`study_iuids` omitted): picks unused cases for this rad (2 on first call, 1 thereafter), records `case_assignments` rows, stamps `incubation_started_at` if first case. Returns activation-data items.

Response shape: `[{ history, rules, dicomData }, ...]`

### 4.4 Grading ([`app/api/grading.py`](app/api/grading.py))
- `POST /api/v1/grade_case` — Returns 202 immediately. Body: `{rad_id, session_id?, study_iuid, candidate_report:{observation, impression}, submitted_at?}`. Response: `{grading_id, status}`. Grading runs in background.
- `GET /api/v1/grade_case/{grading_id}` — Poll for the result (grade, score, rationale, etc.).
- `GET /api/v1/rad/{rad_id}/grades` — All grades for a rad in case-number order.

---

## 5. Database

### 5.1 Hosted? — **NOT YET**
The DB is **not yet hosted**. The plan (per [`scripts/vm-init-borderless.sql`](scripts/vm-init-borderless.sql) and [`.env.example`](.env.example)) is to use an **existing PostgreSQL instance running in a Docker container on a VM** managed by a separate docker-compose. That instance:

- Is reachable on a non-default port (`.env.example` shows `5433`).
- Already runs as user `radar` (per the example `DATABASE_URL`).
- Does **not yet** have the `borderless` database or the `rad_incubation` schema — those must be created via [`scripts/vm-init-borderless.sql`](scripts/vm-init-borderless.sql).

Once that database is created and `alembic upgrade head` runs, the schema below exists.

### 5.2 Connection
- Driver: `postgresql+asyncpg` (SQLAlchemy 2.0 async)
- Connection URL is configured via `DATABASE_URL` (see [§6](#6-environment-variables))
- Connection pool: `pool_pre_ping=True`

### 5.3 Schema: `rad_incubation`
Single Alembic revision: [`alembic/versions/20260424_0001_initial.py`](alembic/versions/20260424_0001_initial.py).

Five tables, all under the `rad_incubation` schema:

#### Table 1: `Study_Groundtruth`  *(the 80-case pool)*
| Column                 | Type                  | Notes                                              |
| :--------------------- | :-------------------- | :------------------------------------------------- |
| study_id (PK)          | INTEGER               | From source data                                   |
| study_iuid (UNIQUE)    | VARCHAR(255)          | DICOM Study Instance UID                           |
| modstudy               | VARCHAR(225)          | Modality + study type                              |
| groundtruth_pathology  | TEXT                  | Free-form prose pathology blob                     |
| modality               | VARCHAR(225)          |                                                    |
| history                | TEXT                  | Clinical history (drives primary indication)       |
| dicom_metadata         | TEXT (JSON string)    | Stored as text, parsed at serve time               |
| rules                  | TEXT (JSON string)    | Activation rules (template structure)              |
| **is_complex**         | BOOLEAN               | Incubation addition — drives future complex quota  |
| **main_pathologies**   | JSONB                 | Pre-classified by LLM; list of strings             |
| **incidental_findings**| JSONB                 | Pre-classified by LLM; list of strings             |
| **classified_at**      | TIMESTAMPTZ           | When pool-classifier ran                           |
| created_at, updated_at | TIMESTAMPTZ           | Defaults to `Asia/Kolkata` now                     |

Indexes: `is_complex`, `classified_at`.

#### Table 2: `rad_state`  *(per-rad incubation state, one row per rad)*
| Column                  | Type                                  | Notes                            |
| :---------------------- | :------------------------------------ | :------------------------------- |
| rad_id (PK)             | VARCHAR(64)                           | External rad identifier          |
| incubation_started_at   | TIMESTAMPTZ                           | Anchors the 7-day window         |
| status                  | ENUM `rad_status`                     | in_progress / completed_80 / timed_out_7_days / suspended_at_20 |
| cases_completed         | INTEGER                               | Bumped after each successful grade |
| created_at, updated_at  | TIMESTAMPTZ                           |                                  |

Indexes: `status`, `incubation_started_at`.

#### Table 3: `case_assignments`  *(which study went to which rad, in what order)*
| Column         | Type        | Notes                                            |
| :------------- | :---------- | :----------------------------------------------- |
| assignment_id  | UUID PK     | `gen_random_uuid()`                              |
| rad_id (FK)    | VARCHAR(64) | → `rad_state.rad_id` ON DELETE CASCADE           |
| study_iuid     | VARCHAR(255)|                                                  |
| study_id       | INTEGER     |                                                  |
| case_number    | INTEGER     | 1..80, position in this rad's sequence           |
| is_complex     | BOOLEAN     | Snapshotted from pool at assignment time         |
| assigned_at    | TIMESTAMPTZ |                                                  |

UNIQUE: `(rad_id, study_iuid)`. Indexes: `rad_id`, `(rad_id, case_number)`.

#### Table 4: `grading_jobs`  *(audit log — one row per graded case)*
| Column                          | Type                | Notes                                          |
| :------------------------------ | :------------------ | :--------------------------------------------- |
| grading_id (PK)                 | UUID                | `gen_random_uuid()`                            |
| rad_id (FK)                     | VARCHAR(64)         | → `rad_state.rad_id` ON DELETE CASCADE         |
| study_iuid                      | VARCHAR(255)        |                                                |
| study_id                        | INTEGER             |                                                |
| case_number                     | INTEGER             |                                                |
| submitted_at                    | TIMESTAMPTZ         | When rad submitted                             |
| status                          | ENUM `grading_status`| queued / running / done / error               |
| grade                           | VARCHAR(4)          | `1`, `2A`, `2B`, `3A`, `3B`                    |
| score_10pt                      | NUMERIC(3,1)        | Fixed lookup from grade                        |
| critical_miss                   | BOOLEAN             | True iff grade in {3A, 3B}                     |
| overcall_detected               | BOOLEAN             |                                                |
| related_to_primary_indication   | BOOLEAN             |                                                |
| llm_raw_json                    | JSONB               | Full LLM response                              |
| llm_rationale                   | TEXT                |                                                |
| llm_model                       | VARCHAR(64)         | e.g. `gemini-2.5-flash-lite`                   |
| ground_truth_snapshot           | JSONB               | Snapshot at submission (audit defensibility)   |
| candidate_snapshot              | JSONB               | Submitted observation + impression             |
| error_message                   | TEXT                | Populated when status=error                    |
| created_at, graded_at           | TIMESTAMPTZ         |                                                |

UNIQUE: `(rad_id, study_iuid)` — idempotent enqueue. Indexes: `(rad_id, status)`, `(rad_id, case_number)`, `study_iuid`.

#### Table 5: `checkpoint_events`  *(20-case gate, 80-case terminal, 7-day timeout)*
| Column                | Type                  | Notes                                           |
| :-------------------- | :-------------------- | :---------------------------------------------- |
| event_id (PK)         | UUID                  | `gen_random_uuid()`                             |
| rad_id                | VARCHAR(64)           |                                                 |
| kind                  | ENUM `checkpoint_kind`| gate_20 / terminal_80 / terminal_7_days        |
| cases_evaluated       | INTEGER               |                                                 |
| avg_score             | NUMERIC(4,2)          |                                                 |
| overall_grade         | VARCHAR(4)            |                                                 |
| quality_met           | BOOLEAN               | True iff overall_grade == "1"                   |
| grade_counts          | JSONB                 | `{"1":n, "2A":n, ...}`                          |
| summary               | TEXT                  | Built by [`summary.py`](app/services/summary.py)|
| callback_status       | ENUM `callback_status`| pending / sent / failed                         |
| callback_attempts     | INTEGER               |                                                 |
| callback_last_error   | TEXT                  |                                                 |
| callback_payload      | JSONB                 | Full payload that was POSTed                    |
| slack_sent            | BOOLEAN               |                                                 |
| slack_last_error      | TEXT                  |                                                 |
| evaluated_at          | TIMESTAMPTZ           |                                                 |

UNIQUE: `(rad_id, kind)` — guarantees each checkpoint fires exactly once per rad. Index: `rad_id`.

### 5.4 Enums (PostgreSQL native ENUMs, all under `rad_incubation` schema)
- `rad_status`: `in_progress`, `completed_80`, `timed_out_7_days`, `suspended_at_20`
- `grading_status`: `queued`, `running`, `done`, `error`
- `checkpoint_kind`: `gate_20`, `terminal_80`, `terminal_7_days`
- `callback_status`: `pending`, `sent`, `failed`

### 5.5 What is currently in the DB?
**Nothing.** The migration has not been run against any live instance yet. The DB does not exist on a host; running the migration is part of the go-live checklist below.

---

## 6. Environment Variables

Copy [`.env.example`](.env.example) to `.env` and fill in. **All variables:**

| Variable                          | Required? | Default                     | Purpose                                                                 |
| :-------------------------------- | :-------- | :-------------------------- | :---------------------------------------------------------------------- |
| `DATABASE_URL`                    | **YES**   | —                           | `postgresql+asyncpg://<user>:<urlencoded-password>@<host>:<port>/borderless`. URL-encode special chars in password (`@`→`%40`, `:`→`%3A`, etc.). |
| `GEMINI_API_KEY`                  | **YES**   | `""` (empty → grader fails) | Google Gemini API key. Without this, `/grade_case` and `/classify` will error. |
| `GEMINI_MODEL`                    | no        | `gemini-2.5-flash-lite`     | Model name passed to the Gemini SDK.                                     |
| `API_AUTH_KEY`                    | **YES (prod)** | `""` (empty → AUTH OFF) | Bare-token clients send in `Authorization` header. **MUST be set in prod.** |
| `SLACK_WEBHOOK_URL`               | recommended | `""`                       | Ops alert webhook for 20/80/7d events. If blank, alerts are skipped (logged warning). |
| `EXTERNAL_CALLBACK_URL`           | required for live | `""`                | Decision-system endpoint we POST checkpoint payloads to. If blank, the call is skipped and `callback_status` stays `pending`. |
| `EXTERNAL_CALLBACK_KEY`           | optional  | `""`                        | Sent as `X-API-Key` header on the external callback POST.               |
| `API_URL`                         | optional  | `http://localhost:8000`     | Self-reference (currently unused in code, kept for forward-compat).     |
| `SEVEN_DAY_JOB_ENABLED`           | no        | `true`                      | Enables the APScheduler 7-day sweep.                                    |
| `SEVEN_DAY_JOB_INTERVAL_MINUTES`  | no        | `60`                        | How often to sweep. 60 min is fine for prod.                            |

> **Security note:** `API_AUTH_KEY` empty silently disables auth. Make sure prod env definitely sets a strong value. There is no CI guard for this.

---

## 7. Pending / To-Do Checklist

### 7.1 Code — what is DONE
- [x] FastAPI app skeleton, lifespan, scheduler, routers all wired ([`app/main.py`](app/main.py)).
- [x] All 5 DB tables modeled in SQLAlchemy + a complete Alembic initial migration.
- [x] Activation API: both UID-lookup and random-pick modes.
- [x] Grading API: enqueue → background worker → LLM call → persist → checkpoint trigger.
- [x] LLM client (Gemini) with retries, JSON enforcement, schema validation.
- [x] Pool ingestion + LLM pre-classification endpoints.
- [x] Checkpoint engine (20/80/7d) — idempotent, fires Slack + external callback.
- [x] 7-day timeout sweeper as APScheduler interval job.
- [x] Slack alert text builder (3 variants: gate, terminal, timeout).
- [x] Bare-token auth dependency.
- [x] DB bootstrap scripts for both fresh Postgres and existing VM Postgres.

### 7.2 Code — what is PENDING
- [ ] **README.md is empty** — currently just `# Borderless-Radiology-backend`. (This handoff doc effectively replaces it; consider linking from README.)
- [ ] **No tests.** `pyproject.toml` declares `pytest` + `pytest-asyncio` as dev deps but the `tests/` directory does not exist. Recommend at minimum: a unit test for `grade_utils` (pure logic), an integration test for `/grade_case` against a test DB.
- [ ] **No Dockerfile / docker-compose for the app.** The DB is dockerized externally on the VM, but this app would run as a separate process. For deployment, you need either a Dockerfile + compose entry, or a systemd unit, or a process manager.
- [ ] **`API_URL` in config is unused** — keep or remove.
- [ ] **No callback retry job.** When the external callback fails, `checkpoint_events.callback_status` is set to `failed` and `callback_last_error` is recorded, but **nothing retries it later.** Either add a periodic retry sweeper or build a manual replay endpoint.
- [ ] **Background grading uses `asyncio.create_task` from inside the request handler** ([`app/api/grading.py:43`](app/api/grading.py#L43)). This works for moderate volume but the task is lost if the process restarts mid-grading. For production-grade reliability, consider moving to a proper queue (RQ, Celery, or a poll-the-`queued`-rows worker).
- [ ] **Complex-case quota is deferred** — `is_complex` is captured but no logic enforces a complex-case-per-hour limit (see [`app/services/activation_service.py:13`](app/services/activation_service.py#L13)). Product hasn't required it yet.
- [ ] **`EXTERNAL_CALLBACK_URL` target is TBD** — confirmed in [`app/services/external_callback.py:3`](app/services/external_callback.py#L3). Need the decision-system URL + auth scheme from product / platform team.
- [ ] **CORS / TrustedHost middleware not configured.** If this is called from a browser (it shouldn't be — n8n is server-side), add `CORSMiddleware`. If exposed publicly behind a domain, set `TrustedHostMiddleware`.
- [ ] **No structured logging / request IDs.** Logs go to stdout with `logging.basicConfig`. Production wants JSON logs + a request ID middleware so checkpoint failures can be traced.
- [ ] **Rate limiting / abuse protection** — none. Acceptable behind n8n on a private network; not acceptable on a public IP.

### 7.3 Database — what is PENDING
- [ ] **Choose & provision the host.** The intent is the existing VM Postgres at `<VM_HOST>:5433` (user `radar`). Confirm credentials and that we have superuser access for `CREATE DATABASE`.
- [ ] **Run [`scripts/vm-init-borderless.sql`](scripts/vm-init-borderless.sql)** against the VM Postgres to create the `borderless` database, `pgcrypto` extension, and `rad_incubation` schema.
- [ ] **Run `alembic upgrade head`** against the new database.
- [ ] **Verify connectivity** from the host that will run the app (port 5433 must be reachable; firewall / security group).
- [ ] **Backup / snapshot policy** — currently piggybacking on whatever the existing VM Postgres has. Confirm with infra.
- [ ] **No staging DB.** Go-live is currently single-environment. Recommend creating a `borderless_staging` DB on the same instance to test migrations.

### 7.4 Pool Data — what is PENDING
- [ ] **Pool data has not been ingested.** Need to:
  1. Get the 80 historical cases (study_iuid + modstudy + ground-truth pathology + history + dicom_metadata + rules + is_complex tag) from the source system.
  2. Format as `list[StudyGroundtruthIngest]` (see [`app/schemas.py:12`](app/schemas.py#L12)).
  3. POST to `/api/v1/study-groundtruth`.
  4. POST to `/api/v1/study-groundtruth/classify` to run the LLM pre-classifier (requires `GEMINI_API_KEY`).
  5. Verify with `GET /api/v1/study-groundtruth` — every row should report `classified: true`.

### 7.5 Integrations — what is PENDING
- [ ] **n8n flow is not wired** to call this backend. n8n is the orchestrator (per the workflow doc) but the actual webhook → backend HTTP calls are owned by the n8n team and have not been implemented yet from this end.
- [ ] **Reporting platform decision API.** Per the workflow doc §8.3, the reporting platform will expose `POST /api/incubation/decision` that n8n calls with the final outcome. **That endpoint is not in this repo** — it lives on the reporting platform side. This backend's `EXTERNAL_CALLBACK_URL` is what feeds n8n / the platform, but the bridge needs to be defined.
- [ ] **Slack channel + webhook.** Need the actual ops channel and webhook URL.
- [ ] **Decide on the "outcome" mapping.** This backend produces `quality_met` (bool) + grade summary. The full outcome (`ELIGIBLE / BLOCK_A / BLOCK_B / BLOCK_C` per workflow doc §8.2) requires combining quality with Punctuality / Regularity / Consistency from the slot-commitment system. **That combination logic is NOT in this backend** — it's expected to live in n8n (or wherever the decision is finalized).

### 7.6 Ops / Observability — what is PENDING
- [ ] No metrics endpoint (Prometheus / OpenMetrics).
- [ ] No `/ready` distinct from `/health` (current `/health` does not check DB).
- [ ] No alerting on `grading_jobs.status = 'error'` accumulating.
- [ ] No alerting on `checkpoint_events.callback_status = 'failed'`.

---

## 8. How to Run Locally

```bash
cd "Borderless radiology backend"

# 1. Install deps (uses pyproject.toml)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Create .env
cp .env.example .env
# edit .env: set DATABASE_URL, GEMINI_API_KEY, API_AUTH_KEY (recommended even locally)

# 3. Bootstrap DB (one-shot — needs superuser in PG)
#    Option A: against a fresh local Postgres
./scripts/init_db.sh
#    Option B: against the VM Postgres
docker exec -i postgres psql -U radar -d postgres < scripts/vm-init-borderless.sql

# 4. Apply migrations
alembic upgrade head

# 5. Run the API
uvicorn app.main:app --reload --port 8000

# 6. Smoke test
curl http://localhost:8000/health
# {"status":"ok"}
```

**Sanity script to seed and grade one case:**
```bash
TOKEN="your-api-auth-key"

# ingest one row
curl -X POST http://localhost:8000/api/v1/study-groundtruth \
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '[{"study_id":1,"study_iuid":"1.2.3","modstudy":"CT_CHEST","groundtruth_pathology":"Right lower lobe consolidation."}]'

# classify it (LLM call)
curl -X POST "http://localhost:8000/api/v1/study-groundtruth/classify" \
  -H "Authorization: $TOKEN"

# fetch activation data — also creates rad_state + assignment
curl "http://localhost:8000/api/v1/activation-data/?rad_id=rad_test" \
  -H "Authorization: $TOKEN"

# grade a case
curl -X POST http://localhost:8000/api/v1/grade_case \
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '{"rad_id":"rad_test","study_iuid":"1.2.3","candidate_report":{"observation":"...","impression":"..."}}'
```

---

## 9. How to Deploy / Make it Live

### Pre-flight
1. Get the VM Postgres credentials. Confirm port `5433` is reachable from the app host. URL-encode the password.
2. Get the production `GEMINI_API_KEY`.
3. Generate a strong `API_AUTH_KEY` (e.g. `openssl rand -hex 32`).
4. Get the Slack webhook URL for the ops channel.
5. Get the `EXTERNAL_CALLBACK_URL` + `EXTERNAL_CALLBACK_KEY` from the platform / n8n team (currently TBD).

### Database
1. Run [`scripts/vm-init-borderless.sql`](scripts/vm-init-borderless.sql) against the VM Postgres (one-shot, idempotent).
2. From the app host, with `.env` populated, run `alembic upgrade head`.
3. Verify: `psql -h <VM> -p 5433 -U radar -d borderless -c "\dn"` → `rad_incubation` should be listed.

### Pool ingestion (one-shot)
1. Prepare 80 cases as `StudyGroundtruthIngest` JSON.
2. POST to `/api/v1/study-groundtruth`.
3. POST to `/api/v1/study-groundtruth/classify` (this hits Gemini once per row).
4. Verify each row has `classified: true`.

### App runtime
This repo does not include a Dockerfile. Two simple options:

**Option A — systemd on the VM:**
Create `/etc/systemd/system/borderless-backend.service`:
```
[Unit]
Description=Borderless Radiology Backend
After=network.target

[Service]
WorkingDirectory=/opt/borderless-backend
EnvironmentFile=/opt/borderless-backend/.env
ExecStart=/opt/borderless-backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
User=borderless

[Install]
WantedBy=multi-user.target
```

**Option B — Docker:**
Add a `Dockerfile` (not present yet — pending):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Smoke test in prod
- `GET /health`
- POST a sample `/grade_case` with a real seeded study_iuid; poll `/grade_case/{id}` until status is `done`; confirm Slack alert never fires (we're below 20 cases) and the row appears in `grading_jobs`.

---

## 10. Known Gaps, Risks & Notes

1. **Auth disabled when `API_AUTH_KEY` is empty.** [`app/security.py:16`](app/security.py#L16) silently allows unauthenticated calls. Loud failure on prod misconfig would be safer.
2. **In-process background grading is fragile.** A worker crash mid-job leaves a `running` row that nothing reaps. Recovery: a sweeper that resets stale `running` rows older than X minutes back to `queued`. Not implemented.
3. **No callback retry.** Failed external callbacks live forever as `callback_status='failed'` with no automatic replay.
4. **Random-pick assignment is not transactional with the rad's actual viewer.** If the n8n flow drops the response between assignment and showing the rad the case, the assignment row exists but the rad never saw it. Acceptable since `cases_completed` only increments on actual grade-done, but worth knowing.
5. **`StudyGroundtruth.dicom_metadata` and `.rules` are stored as TEXT (JSON strings)** and parsed on serve. Migrate to JSONB if you ever need to query inside them.
6. **`Asia/Kolkata` timezone is hardcoded** for `Study_Groundtruth.created_at/updated_at`. All other tables use UTC `now()`. Stay aware of this when comparing timestamps.
7. **Single Alembic revision** — fine for now, just remember any future schema change is a new migration.
8. **No `tests/` directory.** First test should be `grade_utils` round-trips and `quality_met`; second should be a happy-path `/grade_case` with a mocked LLM.
9. **The workflow doc references RadStatus `suspended_at_20`** — this is set when gate_20 fails quality. The activation API will return an empty list with a `message="rad is suspended_at_20"`-ish payload (see [`app/services/activation_service.py:88`](app/services/activation_service.py#L88)) — but the API response model does NOT surface `rad_status` / `message`; the items list is just empty. n8n needs to either inspect that emptiness or we extend the response model. **Currently a thin spot.**
10. **`Borderless_Incubation_Workflow.md` is the product spec** and goes deeper into the 4-outcome decision (ELIGIBLE / BLOCK_A / BLOCK_B / BLOCK_C). Note again: that decision composes Quality (this backend) with P/R/C metrics from the slot-commitment system — **the composition is NOT done here.**

---

## 11. Quick Reference

| Question                         | Answer                                                              |
| :------------------------------- | :------------------------------------------------------------------ |
| What does this service do?       | LLM-grades 80 incubation cases per radiologist; fires checkpoints at 20, 80, 7-day. |
| Is it deployed?                  | **No.** Code is complete; DB not provisioned; pool not ingested.    |
| Is the DB hosted?                | **No.** Target is an existing Postgres on a VM at port 5433. Schema not yet created. |
| What DB tables exist?            | `Study_Groundtruth`, `rad_state`, `case_assignments`, `grading_jobs`, `checkpoint_events` (all under schema `rad_incubation`). |
| What's in the DB right now?      | Nothing. Migration has not been applied to any live instance.       |
| What's the LLM?                  | Google Gemini (`gemini-2.5-flash-lite` by default).                 |
| Auth?                            | Bare token in `Authorization` header; key in `API_AUTH_KEY` env.    |
| Public-facing?                   | No — designed to be called by n8n, server-to-server.                |
| What MUST happen to go live?     | (1) Provision DB, (2) Run init SQL + alembic, (3) Ingest+classify pool, (4) Set all env vars including SLACK + EXTERNAL_CALLBACK, (5) Deploy app process, (6) Wire n8n calls. |

---

*Document generated: 2026-04-27. Working directory: `Borderless radiology backend/`. Single source of truth for handoff. Update this doc as state changes.*
