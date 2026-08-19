# DocFlow — Distributed Document Processing Pipeline

Upload a document, get a job id back immediately, poll for the result. The API
never does the slow work — Celery workers do, in separate processes that can
crash and restart without taking the API down.

**Status: Phase 3 complete.** A three-stage ingestion pipeline - text
extraction, OCR fallback for scans, and embeddings into pgvector - with
retries, dead-lettering and idempotency. See [PLAN.md](PLAN.md) for the roadmap.

```
POST /jobs/upload ──> FastAPI ──> Postgres (job row, PENDING)
                         │
                         └──────> Redis queue ──> Celery workers
                                                       │
        ┌──────────────────────────────────────────────┘
        ▼
   extract_text ──(text layer found)──────────> embed ──> DONE
        │                                         ▲
        └──(scanned: no text layer)──> ocr ───────┘

   any stage ──> RETRYING (backoff) ──> back to the same stage
             ──> FAILED       (bad input, no retry)
             ──> DEAD_LETTER  (retries exhausted)

GET /jobs/{id} <── FastAPI <── Postgres
```

A job is the whole pipeline, not one step: a single row advances through the
stages, so "is my document ready?" stays one lookup while every stage reuses
the same retry and dead-letter machinery.

---

## Quick start

```bash
docker compose up -d --build
```

Six containers come up: `postgres`, `redis`, `api`, `worker`, `beat`, `flower`.
Migrations run automatically before the API starts serving.

| Service | URL | Note |
|---|---|---|
| API | http://localhost:8001 | |
| Swagger UI | http://localhost:8001/docs | Easiest way to try an upload |
| Health | http://localhost:8001/health | Checks DB + broker; 503 if either is down |
| Flower | http://localhost:5556 | Queue depth, task history, failures |
| Postgres | `localhost:5433` | user/pass/db: `docflow` |
| Redis | `localhost:6380` | |

`beat` has no port — it is the scheduler that fires the stale-job reaper. Run
exactly one of it; two would double-fire every scheduled task.

Host ports are deliberately non-default (5433/6380/8001) so this stack can run
alongside other local projects. Override them in a `.env` file — see
[.env.example](.env.example).

### Try it

```bash
curl -s -X POST http://localhost:8001/jobs/upload -F "file=@samples/sample.pdf"
```

```bash
curl -s http://localhost:8001/jobs/<job_id>
```

Watch the transitions as they happen:

```bash
docker compose logs -f worker
```

---

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/jobs/upload` | Accepts a PDF, stores it, creates a job, enqueues it. `202` + `job_id` |
| `GET` | `/jobs/{job_id}` | Full record: status, `result`, `error_message`, timestamps |
| `GET` | `/jobs` | Recent jobs, newest first. `?limit=`, `?offset=`, `?status=` |
| `GET` | `/health` | Readiness probe |

Currently accepts `.pdf` only (`415` otherwise), rejects empty files (`400`)
and uploads over `MAX_UPLOAD_BYTES` (`413`).

Uploading a file whose content is already in the pipeline returns **`200` with
`deduplicated: true`** and the original `job_id`, rather than `202`. Nothing new
was accepted, so it is not a "created" response.

### Job statuses

| Status | Meaning |
|---|---|
| `PENDING` | Queued, not yet picked up |
| `PROCESSING` | A worker has it now |
| `RETRYING` | Transient failure; next attempt due at `next_retry_at` |
| `DONE` | Succeeded; output in `result` |
| `FAILED` | Permanent failure — the input is bad, retrying cannot help |
| `DEAD_LETTER` | Gave up after exhausting `max_retries` |

`FAILED` and `DEAD_LETTER` are deliberately distinct: "this file was never
going to work" and "we kept trying and gave up" need different responses, and
mixing them makes the failure list useless.

### Pipeline stages

`stage` names the step currently running (or the one that failed):
`extract_text` -> `ocr` (scans only) -> `embed`.

`stages` on the detail endpoint is the timeline - what ran, how long it took,
how many attempts it needed, and what it produced:

```json
[
  {"stage": "extract_text", "status": "DONE", "duration_ms": 2,
   "detail": {"decision": "no usable text layer -> ocr", "attempts": 1}},
  {"stage": "ocr",   "status": "DONE", "duration_ms": 784, "detail": {"attempts": 1}},
  {"stage": "embed", "status": "DONE", "duration_ms": 248, "detail": {"attempts": 1}}
]
```

That is how a mid-pipeline failure stays legible: the job status says it
failed, `stages` says which step failed and what the earlier ones produced.
Note `started_at` is when the **current stage** started, not the pipeline -
the reaper needs it that way. The timeline is in `stages`.

---

## Layout

```
core/       config, database session, models + state machine, storage      <- shared
api/        FastAPI app, routes, schemas                                   <- producer
worker/     celery_app, tasks (stage runner), extract, ocr, embed          <- consumer
alembic/    migrations
tests/      unit (extract) + integration (full flow)
```

`core/` exists so the API and worker share one definition of a job without
importing each other.

---

## Design decisions worth knowing

**The job row is committed before the task is enqueued.** A worker can pick up
a task within milliseconds; enqueue first and it will look up a row that does
not exist yet. See [api/routes.py](api/routes.py).

**Postgres is the source of truth for job status, not Celery's result
backend.** `GET /jobs/{id}` reads Postgres and never consults `AsyncResult`.
Two sources of truth for "is my job done" will drift.

**The API enqueues by name via `send_task`, not by importing the task.** So the
API image never needs the worker's processing dependencies (pypdf today,
tesseract in Phase 3). The shared vocabulary is the task-name constant in
[worker/celery_app.py](worker/celery_app.py).

**No database transaction is held open across the slow work.** The task uses
three short transactions — claim, then fail-or-finish — so a long extraction
never pins a connection. See [worker/tasks.py](worker/tasks.py).

**Every exit path leaves the job in a terminal status.** A job stuck in
PROCESSING forever is worse than one marked FAILED. If the broker is
unreachable at upload time, the job is marked FAILED and the API returns 503
rather than leaving it PENDING with nothing to pick it up.

**Sync SQLAlchemy everywhere.** Celery's prefork workers are synchronous and
FastAPI runs plain `def` endpoints in a threadpool, so one driver and one
session factory covers both halves — no async/sync bridging.

**Status is `TEXT` + a `CHECK` constraint, not a Postgres enum.** Phase 2 added
`RETRYING` and `DEAD_LETTER`; widening a check constraint is a drop-and-recreate
inside the migration, whereas `ALTER TYPE ... ADD VALUE` cannot run in a
transactional migration at all.

**`file_path` stores an opaque storage key, not an absolute path.** Phase 6
swaps `LocalDiskStorage` for S3 behind the same interface, with no migration.

### Reliability (Phase 2)

**Failures are classified before they are retried.** A corrupt PDF fails
permanently and immediately; a database blip or I/O error is retried with
backoff. Retrying bad input three times just burns the retry budget and a
worker slot for a result that cannot change. See `PERMANENT_ERRORS` in
[worker/tasks.py](worker/tasks.py).

**`acks_late=True`.** The broker acknowledges a message only once the task
finishes, so a SIGKILLed worker's task is redelivered rather than lost. This is
only safe because tasks are idempotent — which is what the terminal-status
guard and the content-hash key provide.

**Redelivery costs a retry.** `task_reject_on_worker_lost` can loop forever for
a task that reliably kills its worker (OOM being the classic). Reclaiming an
orphaned job increments `retry_count`, so the loop terminates in a dead letter
instead of running until someone notices.

**The retry budget lives in Postgres, not Celery.** `max_retries` is a column,
so the budget survives a worker restart and is visible to anyone reading the
database. Celery's own ceiling is disabled (`max_retries=None`) to keep one
source of truth.

**Backoff has jitter.** Without it, a batch of jobs that fail together retries
in lockstep and hammers whatever just recovered.

**Idempotency is keyed on the SHA-256 of the content**, under a *partial*
unique index that excludes `FAILED` and `DEAD_LETTER`. So re-uploading a file
that is queued, running or done returns the original job, while re-uploading
one that failed is allowed a fresh attempt. The index — not the lookup — is the
real guard: a concurrent duplicate upload loses the race with an
`IntegrityError` and is handed the winning job.

**The reaper covers what `acks_late` cannot.** Redis redelivers unacked
messages after the visibility timeout, but a genuinely lost message leaves a
job in `PROCESSING` with nothing left to move it. A Celery Beat sweep every
5 minutes reclaims those, and `SELECT ... FOR UPDATE SKIP LOCKED` keeps
overlapping sweeps from fighting over the same row.

**Time limits are set.** Without `task_time_limit`, one hung task holds a
concurrency slot forever. `STALE_JOB_SECONDS` sits well above the hard limit so
the reaper never steals a job that is legitimately still running.

### Pipeline (Phase 3)

**A job is a pipeline, not a step.** One row advances through the stages rather
than spawning a row per step. The spec suggested a job per step; that would
have needed a second "which document is this?" concept immediately, because the
question people actually ask is "is my document ready?", not "did stage two
finish?". One row keeps that a single lookup and lets every stage reuse the
Phase 2 retry machinery untouched.

**Stages dispatch explicitly instead of using Celery `chain()`.** The route
through the pipeline is not known up front - whether OCR runs depends on what
extraction finds - and a static chain cannot branch on a result. Explicit
dispatch also keeps the commit-then-enqueue rule intact at every hop, and means
a retrying stage does not have to reason about the rest of a chain's state.

**OCR is its own stage, not a fallback inside extraction.** Reading a text
layer is microseconds; OCR is ~1s per page and can be minutes for a long scan.
Mixing a 2ms task and a 60s task under one name makes queue behaviour
unpredictable and would make the Phase 4 priority work considerably harder.

**The OCR decision is per page, not absolute.** `needs_ocr` compares characters
*per page* against a threshold. An absolute threshold passes a 50-page scan
that happens to carry 200 characters of embedded logo text, and fails a
legitimate one-page memo.

**Each stage gets its own retry budget.** `retry_count` resets when the
pipeline advances, so a rocky extraction does not leave the embedding step with
nothing left. The attempt count is written into the `stages` log, so resetting
the counter does not erase the evidence.

**Re-running the embed stage deletes its chunks first.** `acks_late` can
redeliver a finished stage, so a re-run must replace its rows rather than
collide with the `(job_id, chunk_index)` unique constraint. Chunks also cascade
on job delete, so nothing leaves orphaned vectors behind.

**OCR has its own, much longer time limits.** The global limits are sized for
the fast stages; a long scan would be killed mid-page under them.
`STALE_JOB_SECONDS` still sits above `OCR_HARD_TIME_LIMIT` so the reaper cannot
steal a scan that is legitimately still running.

**The embedding model is preloaded when a worker child starts**, via
`worker_process_init`. Loading it lazily put a ~20s (worse under contention)
cold start into the first real job, which defeats the point of a warm pool.
The model is also baked into the image, so startup is a disk read rather than a
download.

---

## Tests

```bash
docker compose exec api pytest -v
```

47 tests: extraction, OCR-routing and chunking unit tests (no DB or broker),
the full upload → DONE flow, Phase 2's reliability behaviour — retry-then-succeed,
dead-lettering, permanent failures skipping retries, backoff growth and jitter,
deduplication, reclaiming orphaned work, the reaper — and Phase 3's pipeline:
digital PDFs skipping OCR, scans routed through it, real embeddings landing in
pgvector at the schema dimension, re-runnable stages, cascade deletes, and a
mid-pipeline failure being attributed to the stage that caused it.

Two of them do real work rather than mocking it: one runs tesseract over a
genuinely rasterised PDF, and one embeds with the real model. Both are worth
the couple of seconds — a mocked OCR test proves nothing about whether
tesseract is actually installed in the image.

Two Celery details that shape how these are written:

`task_always_eager` has **no effect on `app.send_task()`** — it only
short-circuits `Task.apply_async()`. So the producer side is verified by
capturing the send, and the consumer side by invoking the task with `.apply()`.
That also keeps the two halves tested independently, which is how they run.

Under `.apply()`, `self.retry()` re-executes the task **inline** rather than
scheduling it on the broker, and countdowns are skipped. One call therefore
drives a whole retry chain synchronously, which is what makes the retry tests
fast. The database is the assertion target, not the return value.

---

## Common issues

**Worker logs `No job row for <uuid>`** — something enqueued before committing.

**`Stored file is missing`** — the API and worker are not sharing the `uploads`
volume. Both mount it at `/data/uploads`.

**Editing `worker/tasks.py` changes nothing** — the API runs with `--reload`,
the worker and beat do not. `docker compose restart worker beat`.

**Schema changed but the table did not** — migrations run on API startup;
`docker compose restart api`, or `docker compose exec api alembic upgrade head`.

**A re-upload returns an old job instead of processing** — that is
deduplication working. Identical bytes map to the same job unless the previous
one is `FAILED` or `DEAD_LETTER`.

**A job sits in `RETRYING` for a while** — expected. Check `next_retry_at`. With
the defaults (`RETRY_BACKOFF_BASE=4`, `max_retries=3`) the three waits are
roughly 4s, 8s and 16s before jitter; the `RETRY_BACKOFF_MAX` cap of 10 minutes
only comes into play if the retry budget is raised.

**Scheduled tasks fire twice** — more than one `beat` container is running.

**A text PDF got sent to OCR** — its text layer averages under
`OCR_MIN_CHARS_PER_PAGE` (40) characters per page. Sparse documents (a title
page, a mostly-blank form) can trip this. The result is still correct, just
slower.

**`Tesseract is not installed`** — the image was built before Phase 3.
`docker compose build --no-cache` and bring it back up.

**The embed stage reports a dimension mismatch** — `EMBEDDING_MODEL` and
`EMBEDDING_DIM` disagree, or the model changed without a migration. The
`vector(N)` column dimension is fixed at migration time; changing models means
a new migration and re-embedding.

**Running Celery natively on Windows** — the prefork pool does not work; use
`--pool=solo`. Inside Docker (Linux) this is a non-issue.

**Flower's REST API is unauthenticated here** (`FLOWER_UNAUTHENTICATED_API`).
That is fine on localhost; it must not be set on a public deployment.

---

## Next

Phase 4 is scale: separate `high`/`default`/`low` queues routed with `-Q`,
concurrency limits, and rate limiting on the upload endpoint. Now that OCR
exists, the queue-separation argument has teeth — a 60-second scan and a 2ms
extraction should not sit behind one another. [PLAN.md](PLAN.md) has the detail.
