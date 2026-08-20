# DocFlow — Distributed Document Processing Pipeline

Upload a document, get a job id back immediately, poll for the result. The API
never does the slow work — Celery workers do, in separate processes that can
crash and restart without taking the API down.

[![CI](https://github.com/Karan67/DocFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/Karan67/DocFlow/actions/workflows/ci.yml)

**Status: Phase 6 complete.** A three-stage ingestion pipeline - text
extraction, OCR fallback for scans, and embeddings into pgvector - with
retries, dead-lettering, idempotency, priority queues, rate limiting, a live
dashboard, S3 storage and CI. See [PLAN.md](PLAN.md) for the roadmap and
[DEPLOY.md](DEPLOY.md) for the AWS runbook.

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

Eight containers come up: `postgres`, `redis`, `api`, `worker`, `worker-ocr`,
`beat`, `flower`, `frontend`. Migrations run automatically before the API
starts serving.

**Open http://localhost:3001** for the dashboard.

Two worker pools, on purpose:

| Pool | Queues | Concurrency | Why |
|---|---|---|---|
| `worker` | `high`, `default`, `low` | 4 | The fast stages. Drains in strict order, so `high` empties before `default` is touched |
| `worker-ocr` | `ocr` | 1 | OCR only. Tesseract is CPU-bound and already multi-threaded, so stacking it just makes every scan slower |

A 60-second scan can never sit in front of a 2ms text extraction, because the
two never share a queue.

| Service | URL | Note |
|---|---|---|
| **Dashboard** | **http://localhost:3001** | Upload, live job table, stage timeline |
| API | http://localhost:8001 | |
| Swagger UI | http://localhost:8001/docs | Easiest way to try an upload |
| Health | http://localhost:8001/health | Checks DB + broker; 503 if either is down |
| Flower | http://localhost:5556 | Queue depth, task history, failures. Sign in with `FLOWER_USER`/`FLOWER_PASSWORD` (default `admin`/`admin`) |
| Postgres | `localhost:5433` | user/pass/db: `docflow` |
| Redis | `localhost:6380` | |

`beat` has no port — it is the scheduler that fires the stale-job reaper. Run
exactly one of it; two would double-fire every scheduled task.

Concurrency is tunable without editing compose: `WORKER_CONCURRENCY` and
`OCR_CONCURRENCY`.

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
| `GET` | `/stats` | Queue depths and job counts, for the dashboard |
| `GET` | `/health` | Readiness probe |

Currently accepts `.pdf` only (`415` otherwise), rejects empty files (`400`)
and uploads over `MAX_UPLOAD_BYTES` (`413`).

Uploading a file whose content is already in the pipeline returns **`200` with
`deduplicated: true`** and the original `job_id`, rather than `202`. Nothing new
was accepted, so it is not a "created" response.

`POST /jobs/upload` accepts an optional `priority` form field (`high`, `normal`
or `low`). Left unset, uploads over `LARGE_FILE_BYTES` default to `low` — one
40MB scan should not make a queue of one-page invoices wait. An explicit
priority always wins.

The endpoint is rate limited to `RATE_LIMIT_UPLOAD` (default 30/minute) per
client IP, returning `429`. Reads are not limited — polling job status has to
stay free, or the API stops answering the one question it exists to answer.

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
api/        FastAPI app, routes, stats, schemas, limiter                    <- producer
worker/     celery_app, tasks (stage runner), extract, ocr, embed          <- consumer
frontend/   Next.js dashboard (App Router, Tailwind)                       <- UI
flowerauth/ login form + reverse proxy in front of Flower                  <- admin gate
alembic/    migrations
scripts/    container entrypoints
tests/      unit (pure logic) + integration (full flow against Postgres)
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

### Scale (Phase 4)

**Named queues, not Celery's numeric priorities.** Celery's numeric priority
over Redis is implemented as several queue keys under the hood, with fiddly
semantics. Naming the queues makes the routing explicit and — the part that
actually matters — lets a worker pool subscribe to a *subset* of them.

**Urgency and workload class are separate axes.** `high`/`default`/`low` is
urgency; `ocr` is workload class. Collapsing them ("OCR is low priority") would
be wrong: an urgent scan is still urgent, it just must not run on a pool that
fast work depends on.

**Strict ordering needs two settings, not one.** `-Q high,default,low` alone
round-robins between queues, so "priority" would mean nothing;
`queue_order_strategy: "priority"` makes the worker drain them in the order
given. And it only holds with `worker_prefetch_multiplier=1` — a worker holding
a prefetched batch of low-priority work would chew through it before looking at
`high` again.

**OCR ignores priority, deliberately.** With one OCR worker, jumping the queue
only reorders a backlog that is CPU-saturated either way. Priority sub-queues
for OCR would be three more queues buying almost nothing.

**Large uploads self-demote.** Over `LARGE_FILE_BYTES` and with no explicit
priority, a job lands on `low`. The common case is many small documents, and
one large one should not make them wait.

**The rate limiter is Redis-backed, not in-process.** In-memory limiting is a
property of one replica, so four API containers would let through four times
the configured rate.

**Only the write endpoint is limited.** Rate-limiting status polling would
break the core interaction: clients are *told* to poll.

**`X-Forwarded-For` is only trusted when the hop count is declared.** The
header is appended to by each proxy and the client controls the left-hand
entries, so taking the leftmost value - the usual mistake - lets anyone send a
random address and get a fresh quota per request. `TRUSTED_PROXY_COUNT` says
how many proxies are in front, and only that many entries back from the right
are believed. See [api/limiter.py](api/limiter.py).

### Dashboard (Phase 5)

**Queue depth needed a backend endpoint.** The browser cannot read Redis, so
`GET /stats` exposes it - four `LLEN`s and two grouped counts, cheap enough to
poll every two seconds. It is the one metric that answers "are the workers
keeping up".

**Depth counts waiting messages only.** Work already handed to a worker has
been popped off the list, so a saturated system can legitimately show zero.
The dashboard says so on the panel rather than letting the number mislead.

**Both breakdowns are zero-filled server-side.** Returning only non-zero
statuses would make the dashboard's layout jump every time a count reached
zero.

**The stats endpoint degrades instead of failing.** If Redis is unreachable,
queue depths come back `null` with `broker_reachable: false` and the
Postgres-backed half still answers. A dashboard that 500s the moment the broker
blips is worse than one that says which half it cannot see.

**The dashboard polls; it does not stream.** That matches the API's actual
contract - status lives in Postgres and clients are told to poll - and the job
detail view stops polling once a job reaches a terminal state, so a tab left
open goes quiet instead of hammering the API forever.

**The stage timeline is the point of the detail view.** A mid-pipeline failure
shows the job status as failed and the timeline shows *which* step failed, how
long the earlier ones took, and how many attempts each needed.

**`NEXT_PUBLIC_API_BASE_URL` is a build arg, not a runtime env var.** Next
inlines `NEXT_PUBLIC_` values into the client bundle at build time, and it has
to be an address the *browser* can reach - not the Docker-internal
`http://api:8000`.

### Deployment (Phase 6)

**The storage swap needed no migration, which was the whole bet.** `file_path`
has held an opaque key since Phase 1, so switching to S3 changed one file.
`STORAGE_BACKEND=s3` is the entire operation.

| Backend | Used by |
|---|---|
| `local` | shared Docker volume, local development |
| `s3` | MinIO locally, real S3 in production |

Both are tested with the *same* assertions in
[tests/test_storage.py](tests/test_storage.py) — that parity is the claim the
abstraction was making. They run against MinIO rather than a mock, because a
mock would have passed while telling us nothing about whether the endpoint
wiring works or whether `open()` returns something pypdf can seek in. S3's
streaming body is forward-only and a PDF's cross-reference table lives at the
end of the file, so the S3 backend downloads into a seekable buffer.

**No credentials in application config.** boto3 resolves environment variables
in development and an instance role in production. `core/storage.py` never
reads an access key.

**The images are split.** The API is 411MB; the worker is 976MB. Only the
workers need tesseract, poppler and the embedding model, and the API is the
service that scales horizontally.

| Target | Contents | Services |
|---|---|---|
| `api` | shared runtime only | `api` |
| `worker` | + OCR system packages, processing deps, embedding model | `worker`, `worker-ocr`, `beat`, `flower` |
| `dev` | + test dependencies | local compose, CI |

`beat` and `flower` sit on the worker image for a non-obvious reason: Celery's
`imports` setting makes **every** `celery -A` entrypoint load `worker.tasks`,
so they pull in the processing dependencies whether they use them or not.

**The reaper now sweeps `PENDING` as well as `PROCESSING`.** A job committed but
never enqueued — the API dying between the two — was previously stuck forever,
because no worker had ever claimed it. Reviving costs a retry, so a job whose
enqueue keeps failing dead-letters rather than looping.

**Flower sits behind a login page, not HTTP basic auth.** It can revoke and
terminate tasks, so it is an admin surface rather than a status page - but
`--basic_auth` hands the browser its native credential dialog, and Flower's
dashboard refreshes on a timer, so the dialog gets dismissed and redrawn while
you are still typing into it.

So Flower now runs unauthenticated on the internal network and is **not
published to the host at all**. [flowerauth/](flowerauth/main.py) holds the
port instead: a small Starlette app that serves a real form, sets a signed
session cookie, and reverse-proxies everything through. Flower's UI is plain
HTTP - it polls over AJAX and registers no websocket handlers - so a
request/response proxy is enough.

It runs on the lean `api` image, since it imports no Celery. Two details worth
noting: `?next=` is restricted to relative paths so it cannot become an open
redirect into an admin UI, and the session cookie is stripped before requests
are forwarded to Flower, which has no use for it.

---

## Tests

```bash
docker compose exec worker pytest -v
```

Tests run in the **worker** container, not the API one: they drive the full
pipeline, and the API image deliberately does not carry tesseract, poppler or
the embedding model.

120 tests: extraction, OCR-routing, chunking, queue-routing and Flower-auth
unit tests (no DB or broker),
the full upload → DONE flow, Phase 2's reliability behaviour — retry-then-succeed,
dead-lettering, permanent failures skipping retries, backoff growth and jitter,
deduplication, reclaiming orphaned work, the reaper — and Phase 3's pipeline:
digital PDFs skipping OCR, scans routed through it, real embeddings landing in
pgvector at the schema dimension, re-runnable stages, cascade deletes, and a
mid-pipeline failure being attributed to the stage that caused it.

Phase 4 adds queue routing per stage and priority, the reaper requeueing onto
the right queue, priority resolution, and the rate limiter actually returning
429 at the configured threshold.

Two tests do real work rather than mocking it: one runs tesseract over a
genuinely rasterised PDF, and one embeds with the real model. Both are worth
the couple of seconds — a mocked OCR test proves nothing about whether
tesseract is actually installed in the image.

One subtlety if you add tests: the limiter is Redis-backed and its window
outlives a single test, so the `client` fixture disables it. Tests that
exercise limiting turn it back on and clear the window either side.

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

**Scans queue up but never run** — `worker-ocr` is down. Nothing else
subscribes to `ocr`, which is the point, so that queue simply grows.

**Everything is stuck behind one slow job** — a worker is subscribed to queues
it should not be. `worker` must not include `ocr` in its `-Q` list.

**`high` priority makes no difference** — check `queue_order_strategy` is
`priority` in `broker_transport_options`, and that
`worker_prefetch_multiplier` is 1. Either one missing silently degrades
priority to round-robin.

**Flower shows a login page** — it does now, at http://localhost:5556. Default
`admin`/`admin`, set via `FLOWER_USER` / `FLOWER_PASSWORD`. Flower itself is no
longer published to the host; reach it through that page.

**Flower login does not stick** — the session cookie is signed with
`FLOWER_SESSION_SECRET`. If it changes, every existing session is invalidated.

**`ModuleNotFoundError` in beat or flower** — they were built from the `api`
target. Celery loads `worker.tasks` in every `celery -A` entrypoint, so they
need the worker image.

**S3 tests fail locally** — MinIO is not up, or the bucket does not exist.
`docker compose up -d minio minio-init` recreates both.

**429s during local testing** — the limit is per IP per minute and its window
lives in Redis. Inspect with
`docker compose exec redis redis-cli --scan --pattern 'LIMITS:*'`, or wait out
the minute.

**Dashboard shows "Cannot reach the API"** — the browser, not the container, has
to reach the API. Check http://localhost:8001/health directly. If you changed
`NEXT_PUBLIC_API_BASE_URL`, the frontend needs a rebuild, not a restart: the
value is inlined at build time.

**Dashboard is empty but jobs exist** — a stale bundle pointing at the wrong
API origin. `docker compose build frontend && docker compose up -d frontend`.

**A job sits in `PENDING` forever** — the row was committed but never enqueued,
which in practice only happens if the API dies between the two. The reaper
sweeps `PROCESSING`, not `PENDING`, so nothing rescues it. See PLAN.md.

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

All six phases are built. The remaining step is running the AWS deploy itself,
which needs an AWS account and creates billable resources — the runbook is
[DEPLOY.md](DEPLOY.md).
