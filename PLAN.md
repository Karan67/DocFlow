# DocFlow — Build Plan

Target CV bullet, one clause added per phase:

> *"Built a distributed job processing system (FastAPI, Celery, Redis, PostgreSQL)
> handling asynchronous document processing with retry-with-backoff, dead-letter
> handling, and priority queues; deployed on AWS with a live monitoring dashboard."*

---

## Phase 0 — Scaffold ✅ done

Compose stack with five services: `postgres`, `redis`, `api`, `worker`, `flower`.
(Phase 2 added a sixth, `beat`, to schedule the reaper.)

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

## Phase 2 — Reliability ✅ done

Deliberately **before** the OCR/embedding work: this is the part that carries
the CV bullet, all of it is testable with a task that raises on demand, and OCR
drags a ~300MB tesseract dependency into the image that would slow every
rebuild while iterating on retry logic.

- ✅ Exponential backoff with full jitter; `RETRYING` status, `next_retry_at`,
  and a `retry_count` that increments
- ✅ Failures classified: bad input goes straight to `FAILED` without burning
  retries; transient errors back off. This was **not** in the original spec and
  turned out to be the most useful decision in the phase
- ✅ Dead-letter as its own status rather than a flag on `FAILED`, so "we gave
  up" is queryable separately from "this input never had a chance"
- ✅ **`acks_late=True`** plus `task_reject_on_worker_lost`, with reclaims
  charged against the retry budget so redelivery cannot loop forever
- ✅ Idempotency keyed on the **SHA-256 of the file bytes**, under a *partial*
  unique index excluding failed jobs, so a genuine retry after failure still
  works. `IntegrityError` is caught so a concurrent duplicate loses gracefully
- ✅ A Celery Beat reaper sweeping jobs orphaned in `PROCESSING`, using
  `FOR UPDATE SKIP LOCKED`
- ✅ Task time limits, so one hung task cannot hold a slot forever

Schema (migration `0002`): `idempotency_key`, `next_retry_at`, a widened
`ck_jobs_status`, the partial unique index, and a partial index for the sweep.

**Done when:** the retry ladder, dead-lettering and dedup all hold. ✅ Verified:
27 tests green, plus an end-to-end run through the real broker — a job forced
into orphaned `PROCESSING` was reclaimed by the reaper, requeued, and completed
by the live worker as "attempt 2/4".

Deferred deliberately: fault injection in production code. The retry paths are
covered by tests that monkeypatch the extractor, so nothing test-only ships in
the worker.

## Phase 3 — Real processing ✅ done

- ✅ OCR via `pytesseract` + `poppler-utils`, as its own pipeline stage rather
  than a fallback inside extraction — a 2ms task and a 60s task should not
  share a name
- ✅ Routing decided **per page**: `needs_ocr` compares chars/page against a
  threshold, so a 50-page scan carrying 200 chars of logo text still gets OCR'd
- ✅ Embeddings via `fastembed` (ONNX on CPU — no torch, so the image stays in
  the hundreds of MB) into pgvector, with an HNSW cosine index ready for search
- ✅ Stages dispatch explicitly instead of Celery `chain()` — the route is not
  known up front, and a static chain cannot branch on a result
- ✅ A mid-pipeline failure is attributed to its stage: job status says it
  failed, the `stages` log says where, and earlier stages' output survives
- ✅ Per-stage retry budgets, with attempt counts preserved in the stage log
- ✅ OCR-specific time limits; the embedding model preloaded at worker start

Schema (migration `0003`): `stage`, `stages`, the `vector` extension, and a
`document_chunks` table with an HNSW index.

**Done when:** a scan and a digital PDF both come out the far end correctly.
✅ Verified: 47 tests green, plus live runs through the real broker — a
born-digital PDF took the `extract_text → embed` path, and an image-only PDF
took `extract_text → ocr → embed`, recovering 351 characters from pure pixels
and landing a 384-dimensional vector in pgvector. Warm pipeline: 2ms + 784ms +
248ms.

Two findings worth keeping:
- The first cold run took 67s (OCR) + 58s (embed) because the embedding model
  loaded lazily while OCR competed for CPU. Preloading in `worker_process_init`
  cut the warm pipeline to about a second.
- The original test fixtures put one short line per page — 33 chars/page, below
  the 40 threshold — so they were correctly classified as scans. The threshold
  was right; the fixtures were unrealistic. They now build realistic pages.

## Phase 4 — Scale ⬅ next

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
| `autoretry_for` + `retry_backoff` | explicit `self.retry()` with failures classified first | `autoretry_for` retries *everything*, including corrupt input that can never succeed. Classifying first is the difference between a retry policy and a busy-wait |
| "after `max_retries`, mark permanently FAILED" | a distinct `DEAD_LETTER` status | "we gave up" and "this input was never going to work" need different responses; merging them makes the failure list unactionable |
| plain unique index on `idempotency_key` | *partial* unique index excluding failed jobs | A plain unique index would permanently block re-uploading a file whose job failed |
| "embedding generation as a second job type" | one job row that advances through stages | The question people ask is "is my document ready?", not "did stage two finish?". A row per step needs a second grouping concept immediately |
| Celery `chain()` | explicit stage dispatch | Whether OCR runs depends on what extraction finds; a static chain cannot branch on a result |
| OCR as a fallback inside `extract_text` | OCR as its own stage | Reading a text layer is microseconds, OCR is seconds per page. One name for both makes queue behaviour unpredictable |
