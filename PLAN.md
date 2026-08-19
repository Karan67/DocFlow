# DocFlow — Build Plan

Target CV bullet, one clause added per phase:

> *"Built a distributed job processing system (FastAPI, Celery, Redis, PostgreSQL)
> handling asynchronous document processing with retry-with-backoff, dead-letter
> handling, and priority queues; deployed on AWS with a live monitoring dashboard."*

---

## Phase 0 — Scaffold ✅ done

Compose stack with five services: `postgres`, `redis`, `api`, `worker`, `flower`.

Flower was pulled forward from Phase 5 — it is ~10 lines of compose and turns
Phase 1 debugging from guesswork into a live view of tasks landing.

Host ports 5433 / 6380 / 8001 / 5556 so the stack coexists with other local
projects. Image pins `python:3.12-slim` (Celery 5.x does not officially support
3.14 yet).

**Done when:** `docker compose up` yields five healthy containers and Flower
shows the worker connected. ✅

## Phase 1 — Prove the loop ✅ done

- `jobs` table via Alembic migration
- `POST /jobs/upload` → save file → **commit** → enqueue → `202` + `job_id`
- `GET /jobs/{job_id}`, `GET /jobs`
- `extract_text_task` using `pypdf`, deliberately trivial logic
- Explicit state machine: `PENDING → PROCESSING → DONE | FAILED`

**Done when:** upload a PDF, poll, watch it reach DONE with real text in
`result`. ✅ Verified end-to-end, plus: worker stopped mid-flight → API still
accepts uploads, jobs buffer in Redis as PENDING, and drain when the worker
returns.

---

## Phase 2 — Reliability ⬅ next

Deliberately **before** the OCR/embedding work: this is the part that carries
the CV bullet, all of it is testable with a task that raises on demand, and OCR
drags a ~300MB tesseract dependency into the image that would slow every
rebuild while iterating on retry logic.

- `autoretry_for` + `retry_backoff` + `retry_jitter`; a `RETRYING` status and
  `retry_count` that actually increments
- Dead-letter: past `max_retries`, settle as terminal `FAILED` with the
  traceback in `error_message`
- **`acks_late=True`** so a SIGKILLed worker's task returns to the queue instead
  of vanishing — the reliability question interviewers actually ask
- Idempotency keyed on the **SHA-256 of the file bytes**, unique-indexed.
  `core/storage.py` already computes it on upload. This is also what makes
  `acks_late` safe, since re-delivered tasks must be idempotent
- A Celery Beat reaper that resets jobs stuck in `PROCESSING` past a threshold —
  otherwise a dead worker's jobs sit there forever

Schema additions: `idempotency_key`, and widen the `ck_jobs_status` constraint.

## Phase 3 — Real processing

- OCR via `pytesseract` (needs `tesseract-ocr` + `poppler-utils` in the image)
- Second job type: embedding generation
- Chain them with Celery `chain()` — and decide deliberately how a mid-chain
  failure is represented in the parent job's status

## Phase 4 — Scale

- **Separate queues (`high` / `default` / `low`) routed with `-Q`, not Redis
  priority values.** Celery's numeric priority over Redis is implemented as
  multiple queue keys and is genuinely quirky; dedicated queues are what
  production systems do and are far easier to defend in an interview.
  `task_default_queue` is already set, so this is additive.
- `worker_prefetch_multiplier=1` — already set. Without it one worker
  greedily prefetches a batch of long tasks while another sits idle.
- Rate limit the upload endpoint (`slowapi`, Redis-backed)

## Phase 5 — Observability & UI

Flower has been running since Phase 0, so this is the Next.js dashboard on
`GET /jobs`: upload form, live status table, retry counts. CORS is already
configured for `localhost:3001`.

Keeping the frontend out of Phases 0–4 was deliberate — curl, Swagger and
Flower cover everything until there is something worth showing.

## Phase 6 — Deploy

- S3 behind the existing `Storage` interface — a one-file change by design
- AWS deploy (EC2 for API + worker, S3 for files)
- GitHub Actions CI. Tests already run headless in the container, so this is
  wiring rather than writing.
- Split `requirements-dev.txt` out of the runtime image
- Put auth in front of Flower and drop `FLOWER_UNAUTHENTICATED_API`

---

## Changes made to the original spec

| Original | Built instead | Why |
|---|---|---|
| `celery_app.py` inside `api/` | shared `core/` package | The worker needs the same models, config and session; `worker` importing `api` gets tangled fast |
| Reliability in Phase 3 | moved to Phase 2 | Carries the CV bullet; testable without OCR's heavy dependencies |
| Flower in Phase 5 | moved to Phase 0 | ~10 lines, and it makes Phase 1 debuggable |
| "raw psycopg2 if you want it fast-and-dirty" | SQLAlchemy + Alembic | The schema changes in Phases 2 and 4; a `schema.sql` seeded via `docker-entrypoint-initdb.d` only runs on an empty volume, so every change means wiping the DB |
| Redis as broker *and* status store | Redis broker; Postgres is the only source of truth for status | Two answers to "is my job done" will drift |
| `frontend/` in the Phase 1 tree | deferred to Phase 5 | Keeps UI work from eating backend time |
