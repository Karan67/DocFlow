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
)

#: `celery -A worker.celery_app` looks for an attribute named `app`.
app = celery_app
