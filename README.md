# DocFlow — Distributed Document Processing Pipeline

Upload a document, get a job id back immediately, poll for the result. The API
never does the slow work — Celery workers do, in separate processes that can
crash and restart without taking the API down.

**Status: Phase 1 complete.** One job type (`extract_text`), end-to-end.
See [PLAN.md](PLAN.md) for the full roadmap.

```
POST /jobs/upload ──> FastAPI ──> Postgres (job row, PENDING)
                         │
                         └──────> Redis queue
                                     │
                                     ▼
                              Celery worker ──> PROCESSING ──> DONE | FAILED
                                     │                              │
GET /jobs/{id} <── FastAPI <── Postgres <─────────────────────────────┘
```

---

## Quick start

```bash
docker compose up -d --build
```

Five containers come up: `postgres`, `redis`, `api`, `worker`, `flower`.
Migrations run automatically before the API starts serving.

| Service | URL | Note |
|---|---|---|
| API | http://localhost:8001 | |
| Swagger UI | http://localhost:8001/docs | Easiest way to try an upload |
| Health | http://localhost:8001/health | Checks DB + broker; 503 if either is down |
| Flower | http://localhost:5556 | Queue depth, task history, failures |
| Postgres | `localhost:5433` | user/pass/db: `docflow` |
| Redis | `localhost:6380` | |

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

Phase 1 accepts `.pdf` only (`415` otherwise), rejects empty files (`400`) and
uploads over `MAX_UPLOAD_BYTES` (`413`).

---

## Layout

```
core/       config, database session, models + state machine, storage      <- shared
api/        FastAPI app, routes, schemas                                   <- producer
worker/     celery_app, tasks, extract                                     <- consumer
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

**Status is `TEXT` + a `CHECK` constraint, not a Postgres enum.** Phase 2 adds
`RETRYING`; widening a check constraint is a much simpler migration than
`ALTER TYPE`.

**`file_path` stores an opaque storage key, not an absolute path.** Phase 6
swaps `LocalDiskStorage` for S3 behind the same interface, with no migration.

---

## Tests

```bash
docker compose exec api pytest -v
```

11 tests: extraction unit tests (no DB or broker), plus integration tests
covering upload → PENDING → DONE, the failure path, duplicate delivery, and
input validation.

Note: Celery's `task_always_eager` has **no effect on `app.send_task()`** — it
only short-circuits `Task.apply_async()`. The integration tests therefore stub
the send and invoke the task with `.apply()`, which also keeps the producer and
consumer tested independently.

---

## Common issues

**Worker logs `No job row for <uuid>`** — something enqueued before committing.

**`Stored file is missing`** — the API and worker are not sharing the `uploads`
volume. Both mount it at `/data/uploads`.

**Editing `worker/tasks.py` changes nothing** — the API runs with `--reload`,
the worker does not. `docker compose restart worker`.

**Schema changed but the table did not** — migrations run on API startup;
`docker compose restart api`, or `docker compose exec api alembic upgrade head`.

**Running Celery natively on Windows** — the prefork pool does not work; use
`--pool=solo`. Inside Docker (Linux) this is a non-issue.

**Flower's REST API is unauthenticated here** (`FLOWER_UNAUTHENTICATED_API`).
That is fine on localhost; it must not be set on a public deployment.

---

## Next

Phase 2 is reliability: retry with exponential backoff, dead-letter handling,
`acks_late`, SHA-256 idempotency keys, and a reaper for jobs orphaned by a dead
worker. [PLAN.md](PLAN.md) has the detail.
