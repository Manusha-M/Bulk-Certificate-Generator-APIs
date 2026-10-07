# Bulk Certificate Generator — Backend API

A backend service that takes **one request containing many recipients**, validates it,
and generates **one PDF certificate per recipient** from a single predefined template.
Generation runs in the background; the client polls the job for progress, sees exactly
which recipients succeeded or failed (and why), and downloads the finished PDFs.

```
POST /api/v1/jobs                          -> 202 Accepted, { job_id, status: PENDING }
GET  /api/v1/jobs/{job_id}                 -> status + progress (completed / failed / pending / %)
GET  /api/v1/jobs/{job_id}/certificates    -> per-recipient results, download links, error reasons
GET  /api/v1/certificates/{certificate_id} -> the PDF
```

![Example certificate](docs/example-certificate.png)

---

## Contents

1. [Technology stack](#technology-stack)
2. [Architecture](#architecture)
3. [Processing approach](#processing-approach)
4. [Database design](#database-design)
5. [Setup](#setup)
6. [Environment variables](#environment-variables)
7. [Running with Docker](#running-with-docker)
8. [Running locally](#running-locally)
9. [Database migrations](#database-migrations)
10. [Running tests](#running-tests)
11. [API endpoints and examples](#api-endpoints-and-examples)
12. [Design decisions](#design-decisions)
13. [Limitations and future improvements](#limitations-and-future-improvements)

---

## Technology stack

| Concern | Choice |
|---|---|
| Language | **Python** 3.12+ (Docker image uses 3.13) |
| Web framework | **FastAPI** + Uvicorn |
| Database | **PostgreSQL** 16 |
| ORM | **SQLAlchemy** 2.x (typed `Mapped[...]` models) |
| Migrations | **Alembic** |
| Validation | **Pydantic** v2 (+ `email-validator`) and pydantic-settings |
| PDF generation | **ReportLab** (text, layout) + **Pillow** (template image) |
| Tests | **pytest**, FastAPI `TestClient` (httpx), pypdf (reads the generated PDFs) |
| Packaging | **Docker** + Docker Compose |

## Architecture

```
app/
├── main.py                     create_app(): builds engine, generator, routers, error handlers
├── api/
│   ├── dependencies.py         get_db, get_session_factory, get_certificate_generator (DI)
│   ├── error_handlers.py       one JSON error format for every error
│   └── routes/
│       ├── jobs.py             POST /jobs, GET /jobs/{id}, GET /jobs/{id}/certificates
│       ├── certificates.py     GET /certificates/{id} (PDF download)
│       └── health.py           GET /health
├── core/
│   ├── config.py               Settings from environment / .env
│   ├── errors.py               NotFoundError (404), ConflictError (409), ...
│   └── logging.py
├── db/
│   ├── database.py             engine + session factory
│   ├── models.py               GenerationJob, Certificate, status enums
│   └── migrations/             Alembic (versions/0001_initial_schema.py)
├── schemas/                    Pydantic request/response models
├── services/
│   ├── job_service.py          create job, read job, progress calculation
│   ├── generation_service.py   background processing: claim job, per-certificate loop, finalise
│   ├── certificate_generator.py CertificateGenerator: draws the PDF (no HTTP, no DB)
│   └── certificate_service.py  find a certificate and its file for download
└── utils/
templates/certificate_template.png   the single predefined design (artwork only, no text)
scripts/generate_template.py         how that PNG was drawn (Pillow)
tests/                               pytest suite (runs against real PostgreSQL)
```

**Layering.** Routes only translate HTTP to service calls and build responses.
Services hold the business rules. `CertificateGenerator` only knows how to draw a PDF
and where to save it — it is unaware of HTTP and of the database, which makes it easy to
test and easy to replace in tests (a "failing generator" is injected to test failures).

**Dependency injection.** The engine, session factory, settings and generator are created
once in `create_app()` and stored on `app.state`. Routes receive them through FastAPI
`Depends(...)`, so tests override them with `app.dependency_overrides` instead of patching
globals.

## Processing approach

The assignment allows synchronous or background generation. **I chose background
processing with FastAPI's built-in `BackgroundTasks`.**

```
Client ── POST /api/v1/jobs ──► validate (Pydantic) ──► INSERT job + N certificate rows (1 transaction)
                                                          │
       ◄── 202 { job_id, status: PENDING } ───────────────┘   (response is sent first)
                                                          │
                     background task: process_job(job_id) ▼
                     1. claim job  PENDING ─► PROCESSING   (conditional UPDATE)
                     2. for each certificate, in request order:
                          PENDING ─► PROCESSING ─► render PDF ─► COMPLETED (+1 successful_count)
                                                         └── error ─► FAILED + reason (+1 failed_count)
                     3. finalise: COMPLETED | COMPLETED_WITH_ERRORS | FAILED
```

**Why background instead of synchronous:** a request may contain up to 1,000 recipients.
Rendering each PDF takes ~30–70 ms, so a synchronous request could take a minute or more —
long enough for client, proxy or load-balancer timeouts, and the client would learn nothing
until the very end. With a background job the API answers in milliseconds with a `job_id`,
and the client can watch progress and see partial results while generation runs
(measured: 500 certificates in ~34 s in Docker, with the `POST` answered in ~0.4 s).

**Why sequential inside the job:** certificates of one job are generated one after another.
That keeps database session handling and file writes trivially safe and easy to reason
about, and it is already fast enough for the expected volume.

**Jobs never starve the API.** The background task (`run_job_in_background`) runs the
job in a worker thread under its **own** `anyio.CapacityLimiter`
(`MAX_CONCURRENT_JOBS`, default 2). At most that many jobs generate at the same time; extra
jobs simply wait, still `PENDING`. Because the limiter is separate from the thread pool
FastAPI uses for request handlers, progress polling, new submissions and `/health` stay
responsive no matter how many jobs are queued. (Without this, each running job would hold
one of the 40 shared request threads for its whole duration.)

**Why not Celery/RabbitMQ/Redis/Kafka:** see [Design decisions](#why-no-queue-infrastructure).

### Transaction boundaries

The background task **never reuses the request's database session** (that session is
closed when the request ends). It opens its own short transactions from the session
factory:

| Step | Transaction |
|---|---|
| Claim the job | `UPDATE generation_jobs SET status='PROCESSING' WHERE id=:id AND status='PENDING'` |
| Start a certificate | mark it `PROCESSING` |
| Render the PDF | **no transaction open** while the file is written |
| Record the result | certificate `COMPLETED`/`FAILED` **and** job counter `+1`, in one transaction |
| Finalise | recount from certificate rows, set final job status |

Because every certificate result is committed separately, **a failure never rolls back
certificates that already succeeded**. Counters are incremented in SQL
(`successful_count = successful_count + 1`), so the database does the arithmetic atomically.

### Failure isolation

Each certificate is wrapped in its own `try/except`:

| Error | Recorded `error` (shown to the client) | Logged |
|---|---|---|
| `CertificateGenerationError` (expected, e.g. a character the font cannot draw) | its message, e.g. "The recipient name contains characters that the certificate font cannot display" | warning |
| `OSError` (disk full, permissions, …) | "Could not write the certificate file" | full traceback |
| any other exception | "Unexpected error while generating the certificate" | full traceback |
| database error while saving a success | "Could not save the certificate result" (the orphan PDF is deleted) | full traceback |

The loop then continues with the next recipient. Nothing is swallowed silently: every
failure is stored on the certificate row **and** logged with the job and certificate IDs.
Internal details (paths, stack traces) go to the logs only.

If something outside the per-certificate handling raises unexpectedly (a bug, a brief
database error between steps), a last-resort handler marks the job `FAILED` with
"Job processing stopped unexpectedly" and marks its unfinished certificates `FAILED`.
That handler itself needs the database: if the database is completely down, or the
process is killed, the job stays `PROCESSING` — see
[Limitations](#limitations-and-future-improvements).

### Final job status

| Outcome | Job status |
|---|---|
| every certificate generated | `COMPLETED` |
| some generated, some failed | `COMPLETED_WITH_ERRORS` |
| none generated | `FAILED` (`error`: "All certificates in this job failed to generate") |

### No duplicate processing

The job is claimed with a **conditional UPDATE** (`... WHERE status = 'PENDING'`).
PostgreSQL guarantees that only one caller gets `rowcount = 1`; anyone else (a duplicated
background task, a retry) sees `0` and stops. Each certificate is likewise only processed
while it is `PENDING`. A unique constraint on `(job_id, position)` guarantees exactly one
row per submitted recipient. The test suite runs `process_job` twice on the same job and
checks that nothing is generated twice.

## Database design

```
generation_jobs                                   certificates
───────────────────────────────                   ─────────────────────────────────────────
id                 uuid  PK                ┌───<  id               uuid  PK
status             job_status enum         │      job_id           uuid  FK -> generation_jobs.id
certificate_title  varchar(100)            │                             ON DELETE CASCADE
course_name        varchar(200)            │      position         int   (index in the request)
organization_name  varchar(150)            │      recipient_name   varchar(100)
event_date         date                    │      recipient_email  varchar(320)
total_recipients   int                     │      status           certificate_status enum
successful_count   int                     │      file_path        varchar(255) NULL  (never exposed)
failed_count       int                     │      error_message    text NULL
error_message      text NULL               │      created_at       timestamptz
created_at         timestamptz             │      completed_at     timestamptz NULL
started_at         timestamptz NULL        │
completed_at       timestamptz NULL  ──────┘  one job : many certificates
```

* **GenerationJob** — one row per bulk request. Holds the certificate details shared by all
  recipients (title, course, organisation, date), the counters used for progress, and the
  lifecycle timestamps.
* **Certificate** — one row per submitted recipient, created together with the job. It
  holds the recipient-specific data and that recipient's own result: status, the generated
  file (relative path) or the failure reason.

Statuses: job `PENDING → PROCESSING → COMPLETED | COMPLETED_WITH_ERRORS | FAILED`;
certificate `PENDING → PROCESSING → COMPLETED | FAILED`. Both are PostgreSQL enum types.

**Indexes**
* `ix_certificates_job_id_status (job_id, status)` — "all certificates of job X" and
  "failed certificates of job X" (`?status=FAILED`), and the per-status counts.
* `uq_certificates_job_id_position (job_id, position)` — uniqueness, also serves ordering.
* `ix_generation_jobs_status (status)` — finding jobs by state (e.g. stuck jobs).

**Constraints** (the database itself rejects impossible states)
* `total_recipients > 0`; counters `>= 0`; `successful_count + failed_count <= total_recipients`
* a `COMPLETED` certificate must have a `file_path`; a `FAILED` one must have an `error_message`
* one certificate per `(job_id, position)`; `position >= 0`
* foreign key with `ON DELETE CASCADE`

UUIDs are used for all public identifiers, so IDs cannot be guessed or enumerated.

## Setup

Prerequisites: **Docker** (for PostgreSQL or the whole stack) and **Python 3.12+** (only
for running locally / running tests).

```bash
git clone <repository-url> bulk-certificate-generator
cd bulk-certificate-generator
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

## Environment variables

All settings come from environment variables (or `.env`, see `.env.example`). Nothing
environment-specific is hard-coded.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — (required) | SQLAlchemy URL, e.g. `postgresql+psycopg://postgres:postgres@localhost:5432/certificates` |
| `TEST_DATABASE_URL` | — (required for tests) | Separate database for pytest; **name must end in `_test`** |
| `OUTPUT_DIR` | `generated` | Where PDFs are written (`<OUTPUT_DIR>/<job_id>/<certificate_id>.pdf`) |
| `APP_ENV` | `development` | `development`, `test` or `production` |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `MAX_CONCURRENT_JOBS` | `2` | How many jobs generate certificates at the same time |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `certificates` | Used by `docker-compose.yml` |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL. Change it (and the URLs) if 5432 is taken |
| `API_PORT` | `8000` | Host port for the API container |

`.env` is git-ignored. The credentials in `.env.example` are local development defaults
only — never put real secrets in the repository.

## Running with Docker

```bash
docker compose up --build
```

This starts PostgreSQL and the API. The API container waits until PostgreSQL reports
healthy (`depends_on: condition: service_healthy`), runs `alembic upgrade head`, then starts
Uvicorn. Open:

* API docs (Swagger UI): <http://localhost:8000/docs>
* ReDoc: <http://localhost:8000/redoc>
* OpenAPI schema: <http://localhost:8000/openapi.json>
* Health check: <http://localhost:8000/health>

Generated PDFs are stored in the `generated_certificates` Docker volume.
Stop with `docker compose down` (add `-v` to also delete the database and PDFs).

## Running locally

```bash
# 1. Start only PostgreSQL (it also creates the certificates_test database on first start)
docker compose up -d db

# 2. Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# 3. Create the schema
alembic upgrade head

# 4. Start the API (auto-reload on code changes)
uvicorn app.main:create_app --factory --reload
```

The app uses an application factory (`create_app`), hence `--factory`.

> On Windows, prefer `127.0.0.1` over `localhost` in the database URLs: `localhost` may
> resolve to IPv6 first, which adds a noticeable delay to every new connection.

## Database migrations

The schema is managed only by **Alembic** — no manual SQL.

```bash
alembic upgrade head                              # create / update the schema
alembic downgrade base                            # drop everything (dev only)
alembic revision --autogenerate -m "describe change"   # after changing app/db/models.py
alembic check                                     # verify models and migrations agree
```

`app/db/migrations/env.py` reads `DATABASE_URL` from the environment, so `alembic.ini`
contains no credentials. The initial migration is
`app/db/migrations/versions/0001_initial_schema.py`. In Docker, `alembic upgrade head` runs
automatically before the API starts. The test suite also builds its schema by running the
migrations (down to `base`, then up to `head`), and a test asserts that the tables,
columns, types, indexes, unique and foreign keys built by the migration match the ORM models
(CHECK constraints, which Alembic does not compare, have their own tests).

## Running tests

Tests run against **real PostgreSQL** (not SQLite), so enum types, constraints and
transactions behave as in production.

```bash
docker compose up -d db                  # creates certificates_test on first start
pytest
```

`TEST_DATABASE_URL` is read from the environment or `.env`. **Safety:** the suite refuses
to run unless the database name ends in `_test`, so it can never wipe your development
database. Tables are truncated before each test; generated files go to a temporary
directory.

> If your PostgreSQL volume was created before `docker/postgres/init-test-db.sql` existed,
> create the test database once:
> `docker compose exec db createdb -U postgres certificates_test`

What is covered (110 tests, ~20 s):

| File | Covers |
|---|---|
| `test_jobs.py` | job creation (202, `Location` header), job + one row per recipient, initial `PENDING` state, normalisation, a 25-recipient bulk job |
| `test_validation.py` | empty / missing recipients, missing or blank name, name too long or not a string, invalid / missing email, control characters and lone surrogates, unknown fields, bad dates, field lengths, shared texts the font cannot draw, duplicate emails, > 1000 recipients, malformed JSON, errors point to the exact recipient index, Unicode NFC normalisation |
| `test_certificate_generation.py` | a real PDF is created, not empty, one page; recipient name, course, title, organisation, date and certificate ID are **extracted from the PDF text** and checked; UUID-only filenames (path traversal attempt in the name); long texts; unsupported characters; a failed write leaves no partial file |
| `test_job_status.py` | `PENDING`, `PROCESSING` (observed from inside the run), `COMPLETED`; live progress 0 → 25 → 50 → 75 %; progress maths; final-status rules; 404 / 422; a job can be claimed only once; a job claimed by another worker or already finished is not processed again; the concurrent-job limit is respected |
| `test_failure_handling.py` | **A succeeds, B fails, C succeeds** → `COMPLETED_WITH_ERRORS`, reason stored; failure without mocks (unsupported characters); all fail → `FAILED`; disk and unexpected errors recorded without leaking details; a DB error saving one result fails only that certificate; a job-level crash marks the job `FAILED` |
| `test_certificate_retrieval.py` | PDF download (content type, filename, content), 404, 409 for failed and for not-yet-generated certificates, missing file → 500, a real file outside `OUTPUT_DIR` is never served, download links only for completed certificates, no file paths in responses |
| `test_models.py` | relationships, cascade delete, foreign key, every check/unique constraint, migration matches models |
| `test_api.py` | `/docs`, `/redoc`, `/openapi.json`, health check, consistent 404/405 (with `Allow` header)/500 error format, database errors and unexpected errors return generic messages |

## API endpoints and examples

All errors share one shape:

```json
{ "error": { "code": "not_found", "message": "Job not found" } }
```

Validation errors add `details` with the exact field, e.g. `body.recipients.2.email`.

### 1. Create a job — `POST /api/v1/jobs`

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "certificate_title": "Certificate of Completion",
    "course_name": "Advanced Python Backend Development",
    "organization_name": "Example Academy",
    "event_date": "2026-10-07",
    "recipients": [
      { "name": "John Doe",   "email": "john@example.com" },
      { "name": "Jane Smith", "email": "jane@example.com" }
    ]
  }'
```

`202 Accepted` (also sets a `Location` header to the status URL):

```json
{
  "job_id": "6d819d07-9e05-4218-ba87-fd606e33a7fc",
  "status": "PENDING",
  "total_recipients": 2,
  "status_url": "/api/v1/jobs/6d819d07-9e05-4218-ba87-fd606e33a7fc",
  "certificates_url": "/api/v1/jobs/6d819d07-9e05-4218-ba87-fd606e33a7fc/certificates"
}
```

Validation rules:

| Field | Rule |
|---|---|
| `certificate_title` | 3–100 characters, drawable by the certificate font |
| `course_name` | 2–200 characters, drawable by the certificate font |
| `organization_name` | 2–150 characters, drawable by the certificate font |
| `event_date` | ISO date `YYYY-MM-DD` |
| `recipients` | 1–1000 items, no duplicate emails |
| `recipients[].name` | 1–100 characters, no control characters |
| `recipients[].email` | valid email address (stored lower-cased) |

Text is trimmed, repeated whitespace collapsed and Unicode normalised to composed form
(NFC). Unknown fields are rejected.

**Two kinds of invalid data are handled differently, on purpose:**

1. *Malformed request data* — a missing name, an invalid email, an empty list, or a
   shared text (title, course, organisation) the certificate font cannot draw, which
   would make *every* certificate fail. The whole request is rejected with `422` and an
   error for **every** bad field, with its exact recipient index. Nothing is stored, so the
   client fixes the list and resubmits once, instead of ending up with a half-created job
   for a list that contains typos.
2. *A recipient that is valid but cannot be generated* — e.g. a name with characters the
   certificate font cannot draw, a disk error. Only that recipient is marked `FAILED` with
   a reason; all other recipients are still generated.

### 2. Job status and progress — `GET /api/v1/jobs/{job_id}`

```bash
curl http://localhost:8000/api/v1/jobs/<JOB_ID>
```

While running:

```json
{
  "job_id": "…", "status": "PROCESSING", "is_finished": false,
  "total": 500, "completed": 228, "failed": 0, "pending": 272, "progress_percentage": 45.6,
  "certificate_title": "Certificate of Completion", "course_name": "…",
  "organization_name": "…", "event_date": "2026-10-07",
  "error": null,
  "created_at": "2026-10-07T11:42:13.238959Z", "started_at": "2026-10-07T11:42:13.279495Z",
  "completed_at": null,
  "certificates_url": "/api/v1/jobs/…/certificates"
}
```

When finished with one failure:

```json
{ "status": "COMPLETED_WITH_ERRORS", "is_finished": true,
  "total": 3, "completed": 2, "failed": 1, "pending": 0, "progress_percentage": 100.0, … }
```

`progress_percentage = (completed + failed) / total × 100` — it measures how much of the
job has been *processed*, so it reaches 100 % when the job finishes even if some failed.
`pending` includes the certificate currently being rendered.

### 3. Per-recipient results — `GET /api/v1/jobs/{job_id}/certificates`

```bash
curl http://localhost:8000/api/v1/jobs/<JOB_ID>/certificates
curl "http://localhost:8000/api/v1/jobs/<JOB_ID>/certificates?status=FAILED"   # only failures
```

```json
{
  "job_id": "6d819d07-…", "job_status": "COMPLETED_WITH_ERRORS", "count": 3,
  "certificates": [
    { "id": "ed53033d-…", "recipient_index": 0, "recipient_name": "John Doe",
      "recipient_email": "john@example.com", "status": "COMPLETED",
      "download_url": "/api/v1/certificates/ed53033d-…", "error": null,
      "completed_at": "2026-10-07T11:42:13.413998Z" },
    { "id": "30420f7f-…", "recipient_index": 1, "recipient_name": "李雷",
      "recipient_email": "li@example.com", "status": "FAILED", "download_url": null,
      "error": "The recipient name contains characters that the certificate font cannot display",
      "completed_at": "2026-10-07T11:42:13.439543Z" },
    { "id": "afc35e87-…", "recipient_index": 2, "recipient_name": "Jane Smith", "status": "COMPLETED",
      "download_url": "/api/v1/certificates/afc35e87-…", "error": null, … }
  ]
}
```

`download_url` is only present for `COMPLETED` certificates — failed records never get a
fake link. `recipient_index` is the position in the original request.

### 4. Download a certificate — `GET /api/v1/certificates/{certificate_id}`

```bash
curl -OJ http://localhost:8000/api/v1/certificates/<CERTIFICATE_ID>
# saves certificate-<CERTIFICATE_ID>.pdf
```

| Situation | Response |
|---|---|
| generated | `200`, `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="certificate-<id>.pdf"` |
| unknown ID | `404 not_found` |
| generation failed | `409 certificate_generation_failed` |
| not generated yet | `409 certificate_not_ready` |
| record exists but the file is gone | `500 certificate_file_unavailable` (logged; no path in the response) |
| not a UUID | `422 validation_error` |

`409 Conflict` is used because the certificate *exists* — the request conflicts with its
current state — whereas `404` would wrongly suggest the ID is unknown.

### 5. Health — `GET /health`

`200 {"status": "ok", "database": "ok"}` or `503` when the database is unreachable.
Used by the Docker health check.

## Design decisions

**Why FastAPI.** Request validation and the OpenAPI docs come straight from the Pydantic
models, so the documented contract and the enforced contract cannot drift apart. It has
built-in dependency injection (used to swap the generator and database in tests) and
built-in background tasks.

**Why PostgreSQL.** The job/certificate data is relational (one job, many results) and needs
transactions, foreign keys, check constraints, enum types and atomic conditional updates
— the claim-once mechanism relies on them. Tests use PostgreSQL too, so they exercise the
same behaviour as production.

**Why background processing.** Covered in [Processing approach](#processing-approach):
the client gets an answer immediately, can watch progress, and long jobs never hit HTTP
timeouts.

<a id="why-no-queue-infrastructure"></a>
**Why no queue infrastructure (Celery, RabbitMQ, Redis, Kafka).** For this scope they add
two more services to run, configure and monitor, plus a second process model to explain,
without solving a problem we have: one API process generates ~30 certificates per second.
The state that matters already lives in PostgreSQL (statuses, counters, results), so moving
to a real queue later is a contained change: the API would enqueue `job_id` instead of
calling `BackgroundTasks.add_task`, and the worker would call the same
`process_job(job_id)` function — which is already idempotent. The trade-off (jobs are lost if
the process dies, see below) is documented rather than hidden.

**Why ReportLab.** It writes PDFs directly from Python with precise layout control, needs no
browser or system packages (unlike HTML-to-PDF tools), and produces real text in the PDF,
so the tests can extract and verify the recipient's name instead of comparing images.

**Why one certificate template.** The assignment asks for a single predefined design. The
artwork (frame, ornaments, watermark, seal) is one PNG in `templates/`, drawn by
`scripts/generate_template.py` with Pillow. All text is drawn by ReportLab on top, so it
stays sharp and searchable. At startup the PNG is converted to JPEG in memory once;
ReportLab embeds JPEG data as-is, which made each certificate ~6× faster to render and half
the size compared to embedding the PNG.

**How individual failures are isolated.** Per-certificate `try/except`, one transaction
per result, failure reason stored on the row, processing continues. See
[Failure isolation](#failure-isolation).

**How progress is tracked.** Each committed result increments `successful_count` or
`failed_count` on the job in the same transaction, so `GET /jobs/{id}` is a single primary
key lookup and is always consistent with the certificate rows. At the end the counters are
recomputed from the certificate rows as a safety net.

**Filesystem safety.** Files are stored as `<OUTPUT_DIR>/<job_uuid>/<certificate_uuid>.pdf`
— names are built only from UUIDs we generate, never from user input. PDFs are written to a
temporary file and renamed, so a half-written file is never served. The database stores
only the relative path, the API never returns it, and the download endpoint checks that the
resolved path is inside `OUTPUT_DIR` before serving anything.

**Errors and logging.** One JSON error format for every error. Expected errors carry a
safe message; unexpected ones return a generic message while the full traceback is logged
with the job and certificate IDs. Logs never contain recipient names or emails.

## Limitations and future improvements

* **Jobs live in the API process.** If the process stops mid-job, that job stays
  `PROCESSING`/`PENDING`. Production fix: a durable queue + worker (the code is ready for it,
  see above), or a startup task that re-queues stale jobs.
* **Fonts.** The built-in PDF fonts cover Latin (Windows-1252) characters only; other
  scripts (e.g. Chinese, Hindi, some Polish letters) are rejected up front in shared fields
  (`422`) and reported as a per-recipient failure in names, rather than printed as boxes.
  Embedding a Unicode TTF font (e.g. Noto) would remove this limit.
* **Local file storage.** PDFs are on the API's disk/volume. With several API instances,
  use object storage (S3/GCS) and signed download URLs.
* **No authentication**, rate limiting or per-client job ownership — out of scope for the
  assignment; required before exposing the API publicly.
* **No pagination** on `/jobs/{id}/certificates`; fine with the 1,000-recipient cap, needed
  if the cap is raised.
* **No retention policy** for old jobs and PDFs; a periodic clean-up job would be added.
* **Throughput.** Sequential generation does ~15–30 certificates/s per job, with
  `MAX_CONCURRENT_JOBS` jobs in parallel (threads share Python's GIL, so more threads do not
  scale linearly). For more, run several worker processes behind a real queue; the API and
  database design stay the same.
* Possible extras: bulk ZIP download per job, CSV upload, webhook when a job finishes,
  re-try of failed certificates.
