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

## Phase 4 — Scale ✅ done

- ✅ **Named queues (`high` / `default` / `low`) routed with `-Q`**, plus
  `queue_order_strategy: "priority"` so they drain in strict order rather than
  round-robin. Both settings are needed; `-Q` alone means nothing.
- ✅ A **separate `ocr` queue and worker pool** — this was not in the original
  plan and turned out to be the more important half. Urgency and workload
  class are different axes: an urgent scan is still urgent, it just must not
  run on a pool that fast work depends on.
- ✅ Concurrency per pool: 4 for fast stages, 1 for OCR (tesseract is CPU-bound
  and already multi-threaded, so stacking it just slows every scan).
- ✅ `priority` column, an optional upload field, and size-based auto-demotion
  so one large scan does not make a queue of small documents wait.
- ✅ Redis-backed rate limiting on upload only — in-process limiting would be
  per replica, and limiting status polling would break the core interaction.
- ✅ A migration-retry entrypoint, after Postgres recovery mode caused a
  startup race that left the workers unstarted.

Schema (migration `0004`): `priority`, its CHECK constraint, and a
`(priority, status)` index.

**Done when:** an urgent job overtakes a queued backlog, and a slow scan cannot
block fast work. ✅ Verified: 67 tests green, plus a live demonstration — five
low-priority jobs were enqueued *first* and one high-priority job *last*, and
the high one was processed **first**. Separately, a scanned document's OCR
stage ran entirely on the dedicated pool (`queued ocr on ocr`) and handed back
to the fast pool on `high`, and the rate limiter allowed exactly 30 requests
before returning 429.

## Phase 5 — Observability & UI ✅ done

Keeping the frontend out of Phases 0–4 was deliberate — curl, Swagger and
Flower covered everything until there was something worth showing. By Phase 5
there is: four queues, two worker pools, three pipeline stages and six job
statuses.

- ✅ `GET /stats` — queue depths plus job counts. This was not in the plan but
  the dashboard needs it: the browser cannot read Redis, and queue depth is the
  one number that answers "are the workers keeping up".
- ✅ Next.js dashboard (App Router, Tailwind, no runtime deps beyond React):
  upload form with priority, live job table, and a per-job stage timeline.
- ✅ The detail view stops polling once a job is terminal, so a tab left open
  goes quiet rather than hammering the API.
- ✅ `/stats` degrades instead of failing when Redis is down — depths come back
  `null` with `broker_reachable: false` and the Postgres half still answers.
- ✅ Multi-stage Docker build using Next's `standalone` output.

**Done when:** you can upload a document and watch it move without touching a
terminal. ✅ Verified in a real browser: uploaded through the form with
priority `high`, watched the row appear as `Pending → Processing → Failed`
(deliberately malformed PDF), and confirmed the OCR job's detail page renders
the full timeline — `Extract 2ms → "no usable text layer -> ocr"`,
`OCR 784ms → 351 chars`, `Embed 248ms → 1 chunk`. Polling measured at exactly
5 requests per 10s against a 2s interval, no console errors, CORS clean.

## Phase 6 — Deploy ✅ built, deploy pending

- ✅ **S3 behind the existing `Storage` interface** — it really was a one-file
  change, with no migration: `file_path` has held an opaque key since Phase 1.
  Tested against MinIO rather than a mock, with the same assertions run against
  both backends.
- ✅ **Images split**: API 411MB, worker 976MB (was one ~950MB image for
  everything). Only the workers need tesseract, poppler and the model, and the
  API is what scales horizontally.
- ✅ `requirements.txt` / `requirements-worker.txt` / `requirements-dev.txt`,
  so test dependencies never reach a runtime image.
- ✅ **GitHub Actions CI** — tests against real Postgres/Redis/MinIO service
  containers, plus a job that builds all three images to catch a broken
  Dockerfile before it breaks someone's `docker compose up`.
- ✅ Flower behind basic auth; `FLOWER_UNAUTHENTICATED_API` gone.
- ✅ Trusted-proxy handling, so the rate limiter cannot be bypassed by a
  client-supplied `X-Forwarded-For`.
- ✅ **The `PENDING` gap from Phase 5 is closed** — the reaper now revives jobs
  that were committed but never enqueued, charging a retry so the loop is
  bounded.
- ✅ Production compose overlay + [DEPLOY.md](DEPLOY.md) runbook.

**Done when:** the app runs on AWS. ⏳ Everything up to that point is built and
verified — 102 tests green, the S3 backend exercised end-to-end through the
real pipeline (30 objects written to MinIO), the split images built and
running. The deploy itself is not done: it needs an AWS account and creates
billable resources, so it is a deliberate step rather than something the repo
triggers.

Two findings worth keeping:
- Splitting the images revealed that Celery's `imports` setting makes *every*
  `celery -A` entrypoint load `worker.tasks` — so beat and flower need the
  processing dependencies too, not just the worker pools.
- The new `PENDING` sweep hit an illegal transition on its first run:
  `PENDING → DEAD_LETTER` was not in the state machine, because until now
  nothing could reach a terminal state without first being `PROCESSING`.

Deliberately not built: Terraform/CDK (more code than the app for a
single-environment deploy), an autoscaling policy (queue depth is already
exposed via `GET /stats`; wiring it to CloudWatch is real work, not a config
line), and a secrets manager. All noted in DEPLOY.md.

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
| priority queues only | priority queues **plus** a dedicated `ocr` queue and pool | Priority reorders a queue; it does not stop a 60s task blocking a 2ms one. Only a separate pool does that |
| "small files processed before large ones" | large files auto-demoted to `low` | Same intent, expressed as a default rather than a sort: the queue does the ordering, and an explicit priority can still override it |
| dashboard hitting `GET /jobs` only | added `GET /stats` | Queue depth is the headline metric for a queue system and the browser cannot read Redis. `GET /jobs` alone cannot answer "are the workers keeping up" |
| one image for every service | separate `api` / `worker` build targets | The API parses no documents. Shipping tesseract and a 130MB model to the service you scale horizontally is 565MB of dead weight per replica |
| S3 tested with mocks | tested against MinIO | A mocked S3 test passes without proving the endpoint wiring works, or that `open()` returns something pypdf can seek in |
