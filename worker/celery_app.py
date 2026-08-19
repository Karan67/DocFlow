"""Celery application instance, queue topology and routing.

Importing this module is cheap - it pulls in no document-processing libraries
and no ORM. That is deliberate: the API imports it to enqueue work by *name*
via `send_task`, so the API image never needs the worker's heavy dependencies
(pypdf, tesseract, the embedding model).

That is also why the stage and priority values below are plain strings rather
than the enums in `core.models` - importing those would drag SQLAlchemy and
pgvector in here. `tests/test_queues.py` asserts the two stay in step.
"""

from __future__ import annotations

from celery import Celery
from kombu import Queue

from core.config import settings

#: Task names. The single shared vocabulary between producer and consumer.
TASK_EXTRACT_TEXT = "docflow.extract_text"
TASK_OCR = "docflow.ocr"
TASK_EMBED = "docflow.embed"
TASK_REAP_STALE_JOBS = "docflow.reap_stale_jobs"

# --- queue topology ------------------------------------------------------
#
# Two independent concerns, deliberately kept on separate axes:
#
# 1. Urgency        -> high / default / low, drained in strict order.
# 2. Workload class -> ocr, because OCR is ~1000x slower than the other
#                      stages and would otherwise block them head-of-line.
#
# A single worker pool consuming high,default,low handles the fast stages; a
# separate pool owns `ocr` alone, so a 60-second scan can never sit in front of
# a 2ms text extraction.

QUEUE_HIGH = "high"
QUEUE_DEFAULT = "default"
QUEUE_LOW = "low"
QUEUE_OCR = "ocr"

#: Queues the fast worker pool drains, most urgent first.
FAST_QUEUES: tuple[str, ...] = (QUEUE_HIGH, QUEUE_DEFAULT, QUEUE_LOW)

#: Every queue in the topology, in the order a dashboard should show them.
ALL_QUEUES: tuple[str, ...] = (QUEUE_HIGH, QUEUE_DEFAULT, QUEUE_LOW, QUEUE_OCR)

#: JobPriority value -> queue name.
PRIORITY_QUEUES: dict[str, str] = {
    "high": QUEUE_HIGH,
    "normal": QUEUE_DEFAULT,
    "low": QUEUE_LOW,
}

#: Stages that ignore priority and go to their own pool. OCR is here because
#: its cost is bounded by CPU, not by queueing: with one OCR worker, jumping
#: the queue only reorders a backlog that is saturated either way.
DEDICATED_STAGE_QUEUES: dict[str, str] = {"ocr": QUEUE_OCR}


def queue_for(stage: str, priority: str) -> str:
    """Which queue a given stage of a given job belongs on."""
    if stage in DEDICATED_STAGE_QUEUES:
        return DEDICATED_STAGE_QUEUES[stage]
    return PRIORITY_QUEUES.get(priority, QUEUE_DEFAULT)


celery_app = Celery(
    "docflow",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    # The worker loads task definitions on startup; the API never does.
    imports=("worker.tasks",),
    task_default_queue=QUEUE_DEFAULT,
    task_queues=tuple(
        Queue(name) for name in (QUEUE_HIGH, QUEUE_DEFAULT, QUEUE_LOW, QUEUE_OCR)
    ),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Reports a STARTED state to Celery/Flower. Postgres remains the source of
    # truth for business status - see the note in README.
    task_track_started=True,
    # Long tasks: hand out one at a time so a single worker cannot hoard the
    # queue while its siblings idle. Also what makes strict priority ordering
    # meaningful - a worker holding a prefetched batch would drain it before
    # looking at a higher-priority queue.
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
    broker_transport_options={
        # How long Redis waits for an ack before redelivering. Must exceed the
        # longest possible task runtime *and* the longest retry countdown, or
        # work gets redelivered while it is still legitimately pending. OCR's
        # hard limit is the binding constraint here.
        "visibility_timeout": 3600,
        # Drain queues in the order given to `-Q` instead of round-robining
        # between them. Without this, `-Q high,default,low` would give all
        # three equal share and "priority" would mean nothing.
        "queue_order_strategy": "priority",
    },
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_HARD_TIME_LIMIT,
    # Recycle a child after this many tasks to bound any leak in the C
    # libraries the stages call into. Kept high on purpose: each restart
    # reloads the embedding model, which is not free.
    worker_max_tasks_per_child=200,
    # The reaper sweeps up jobs orphaned by a worker that died without its
    # message being redelivered.
    beat_schedule={
        "reap-stale-jobs": {
            "task": TASK_REAP_STALE_JOBS,
            "schedule": float(settings.REAPER_INTERVAL_SECONDS),
            "options": {
                "queue": QUEUE_DEFAULT,
                "expires": settings.REAPER_INTERVAL_SECONDS,
            },
        }
    },
)

#: `celery -A worker.celery_app` looks for an attribute named `app`.
app = celery_app
