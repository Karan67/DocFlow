"""Celery application instance.

Importing this module is cheap - it pulls in no document-processing libraries.
That is deliberate: the API imports it to enqueue work by *name* via
`send_task`, so the API image never needs the worker's heavy dependencies
(pypdf now, tesseract in Phase 3).
"""

from __future__ import annotations

from celery import Celery

from core.config import settings

#: Task names. The single shared vocabulary between producer and consumer.
TASK_EXTRACT_TEXT = "docflow.extract_text"
TASK_REAP_STALE_JOBS = "docflow.reap_stale_jobs"

celery_app = Celery(
    "docflow",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    # The worker loads task definitions on startup; the API never does.
    imports=("worker.tasks",),
    # Explicit queue name shared by producer and consumer (Celery's own default
    # is "celery"); Phase 4 adds "high" and "low" alongside it.
    task_default_queue="default",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Reports a STARTED state to Celery/Flower. Postgres remains the source of
    # truth for business status - see the note in README.
    task_track_started=True,
    # Long tasks: hand out one at a time so a single worker cannot hoard the
    # queue while its siblings idle.
    worker_prefetch_multiplier=1,
    result_expires=60 * 60 * 24,
    broker_connection_retry_on_startup=True,
    # --- Phase 2: reliability ---
    # Acknowledge only after the task finishes. If a worker is SIGKILLed
    # mid-task the message is redelivered instead of vanishing. This REQUIRES
    # tasks to be idempotent, which is what the terminal-status guard and the
    # SHA-256 idempotency key provide.
    task_acks_late=True,
    # Redeliver rather than mark FAILURE when a worker dies mid-task. Note this
    # can loop forever for a task that reliably kills its worker (OOM), so the
    # claim step counts each reclaim against the job's retry budget.
    task_reject_on_worker_lost=True,
    # How long Redis waits for an ack before redelivering. Must exceed the
    # longest possible task runtime *and* the longest retry countdown, or work
    # gets redelivered while it is still legitimately pending.
    broker_transport_options={"visibility_timeout": 3600},
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_HARD_TIME_LIMIT,
    # The reaper sweeps up jobs orphaned by a worker that died without its
    # message being redelivered.
    beat_schedule={
        "reap-stale-jobs": {
            "task": TASK_REAP_STALE_JOBS,
            "schedule": float(settings.REAPER_INTERVAL_SECONDS),
            "options": {"queue": "default", "expires": settings.REAPER_INTERVAL_SECONDS},
        }
    },
)

#: `celery -A worker.celery_app` looks for an attribute named `app`.
app = celery_app
