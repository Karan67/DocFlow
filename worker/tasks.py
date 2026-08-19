"""Celery task definitions - the actual job logic.

Rules this module follows, all of which matter more than they look:

1. Database transactions are short and never wrap the slow work. A transaction
   held open across file I/O pins a connection and blocks other writers.
2. Every exit path leaves the job in a terminal status. A job stuck in
   PROCESSING forever is worse than a job marked FAILED.
3. Failures are classified. Bad input fails permanently and immediately;
   everything else is transient and retried with exponential backoff. Retrying
   a corrupt PDF three times just burns the retry budget and a worker slot.
4. The retry budget lives in Postgres, not in Celery task state, so it survives
   a worker restart and is visible to anyone reading the database.
"""

from __future__ import annotations

import logging
import random
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from core.config import settings
from core.database import session_scope
from core.models import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    IllegalTransition,
    Job,
    JobStatus,
    can_transition,
)
from core.storage import StorageError, get_storage
from worker.celery_app import TASK_EXTRACT_TEXT, TASK_REAP_STALE_JOBS, celery_app
from worker.extract import ExtractionError, extract_text_from_pdf

logger = logging.getLogger(__name__)

#: Failures that will never succeed on a retry - the input itself is the
#: problem. Everything else is treated as transient and retried with backoff.
PERMANENT_ERRORS = (ExtractionError, StorageError)

_MAX_ERROR_CHARS = 8000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(timestamp: datetime | None) -> float:
    """Seconds since `timestamp`. A missing timestamp counts as infinitely old."""
    if timestamp is None:
        return float("inf")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (_now() - timestamp).total_seconds()


def _set_status(job: Job, target: JobStatus) -> None:
    if not can_transition(job.status, target.value):
        raise IllegalTransition(
            f"Job {job.id} cannot move from {job.status} to {target.value}"
        )
    job.status = target.value


def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff with full jitter."""
    delay = min(settings.RETRY_BACKOFF_BASE * (2**attempt), settings.RETRY_BACKOFF_MAX)
    if settings.RETRY_JITTER:
        # Spread simultaneous failures out instead of letting them retry in
        # lockstep and hammer whatever just recovered.
        delay = random.uniform(delay / 2, delay)
    return max(1, int(delay))


def _truncate(detail: str, tb: str) -> str:
    return f"{detail}\n\n{tb}"[:_MAX_ERROR_CHARS]


def _dead_letter(job: Job, reason: str) -> None:
    """Terminal give-up: retries were available and are now exhausted."""
    _set_status(job, JobStatus.DEAD_LETTER)
    job.error_message = reason[:_MAX_ERROR_CHARS]
    job.completed_at = _now()
    job.next_retry_at = None


def _outcome(job_uuid: uuid.UUID, status: str, **extra: Any) -> dict[str, Any]:
    return {"job_id": str(job_uuid), "status": status, **extra}


@dataclass(frozen=True)
class _ClaimResult:
    claimed: bool
    #: Returned straight to Celery when the job could not be claimed.
    payload: dict[str, Any] | None = None
    storage_key: str = ""
    file_name: str = ""
    retry_count: int = 0
    max_retries: int = 0


def _claim_job(job_uuid: uuid.UUID) -> _ClaimResult:
    """Move a job into PROCESSING, or explain why we are not going to."""
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)

        if job is None:
            # Almost always means a task was enqueued before its row was
            # committed. The upload route commits first precisely to avoid it.
            logger.error("No job row for %s - dropping task", job_uuid)
            return _ClaimResult(False, _outcome(job_uuid, "IGNORED", reason="not_found"))

        if job.status in TERMINAL_STATUSES:
            # Duplicate delivery of settled work. Expected under acks_late.
            logger.info("Job %s is already %s - skipping", job_uuid, job.status)
            return _ClaimResult(
                False, _outcome(job_uuid, job.status, reason="already_handled")
            )

        if job.status == JobStatus.PROCESSING.value:
            age = _age_seconds(job.started_at)
            if age < settings.STALE_JOB_SECONDS:
                # Another worker genuinely has this in flight right now.
                logger.warning(
                    "Job %s already in flight for %.0fs - skipping duplicate",
                    job_uuid,
                    age,
                )
                return _ClaimResult(
                    False, _outcome(job_uuid, job.status, reason="in_flight")
                )

            # The previous worker died holding this job. Reclaiming costs a
            # retry, so a task that reliably kills its worker cannot be
            # redelivered forever.
            if job.retry_count >= job.max_retries:
                _dead_letter(
                    job,
                    f"Orphaned in PROCESSING for {age:.0f}s; retry budget "
                    f"exhausted after {job.retry_count} attempt(s)",
                )
                logger.error(
                    "Job %s orphaned and out of retries - dead-lettered", job_uuid
                )
                return _ClaimResult(
                    False,
                    _outcome(
                        job_uuid,
                        JobStatus.DEAD_LETTER.value,
                        reason="reclaim_budget_exhausted",
                    ),
                )

            job.retry_count += 1
            logger.warning(
                "Reclaiming job %s orphaned for %.0fs (attempt %s)",
                job_uuid,
                age,
                job.retry_count + 1,
            )
            # Status is already PROCESSING; only the clock needs resetting.
        elif job.status in CLAIMABLE_STATUSES:
            _set_status(job, JobStatus.PROCESSING)
        else:  # pragma: no cover - the enum has no other members
            raise IllegalTransition(f"Job {job_uuid} in unexpected status {job.status}")

        job.started_at = _now()
        job.next_retry_at = None

        return _ClaimResult(
            True,
            storage_key=job.file_path,
            file_name=job.file_name,
            retry_count=job.retry_count,
            max_retries=job.max_retries,
        )


def _settle_permanent_failure(job_uuid: uuid.UUID, exc: Exception) -> dict[str, Any]:
    """Bad input. No retry - it would fail identically every time."""
    detail = str(exc)
    logger.error("Job %s FAILED permanently: %s", job_uuid, detail)
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is not None and job.status not in TERMINAL_STATUSES:
            _set_status(job, JobStatus.FAILED)
            job.error_message = _truncate(detail, traceback.format_exc())
            job.completed_at = _now()
            job.next_retry_at = None
    return _outcome(job_uuid, JobStatus.FAILED.value, error=detail, retryable=False)


def _retry_or_dead_letter(
    task: Any, job_uuid: uuid.UUID, exc: Exception, claim: _ClaimResult
) -> dict[str, Any]:
    """Transient failure: schedule the next attempt, or give up if out of budget."""
    detail = f"{type(exc).__name__}: {exc}"
    message = _truncate(detail, traceback.format_exc())

    if claim.retry_count >= claim.max_retries:
        logger.error(
            "Job %s exhausted its %s retries - dead-lettering: %s",
            job_uuid,
            claim.max_retries,
            detail,
        )
        with session_scope() as session:
            job = session.get(Job, job_uuid, with_for_update=True)
            if job is not None and job.status not in TERMINAL_STATUSES:
                _dead_letter(job, message)
        return _outcome(
            job_uuid,
            JobStatus.DEAD_LETTER.value,
            error=detail,
            retry_count=claim.retry_count,
        )

    delay = _backoff_seconds(claim.retry_count)
    attempt = claim.retry_count + 1

    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is None or job.status in TERMINAL_STATUSES:
            return _outcome(job_uuid, "IGNORED", reason="settled_elsewhere")
        job.retry_count = attempt
        _set_status(job, JobStatus.RETRYING)
        job.next_retry_at = _now() + timedelta(seconds=delay)
        job.error_message = message

    logger.warning(
        "Job %s failed transiently (%s) - retry %s/%s in %ss",
        job_uuid,
        detail,
        attempt,
        claim.max_retries,
        delay,
    )
    raise task.retry(exc=exc, countdown=delay)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


@celery_app.task(
    name=TASK_EXTRACT_TEXT,
    bind=True,
    # Celery's own ceiling is disabled: the per-job max_retries column is the
    # source of truth, so the budget survives worker restarts and is visible in
    # the database rather than buried in task state.
    max_retries=None,
)
def extract_text_task(self, job_id: str) -> dict[str, Any]:
    """PENDING/RETRYING -> PROCESSING -> DONE | FAILED | DEAD_LETTER."""
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Received a task with an unusable job_id: %r", job_id)
        return {"job_id": job_id, "status": "IGNORED", "reason": "bad_job_id"}

    claim = _claim_job(job_uuid)
    if not claim.claimed:
        return claim.payload  # type: ignore[return-value]

    logger.info(
        "Job %s PROCESSING (%s, attempt %s/%s)",
        job_uuid,
        claim.file_name,
        claim.retry_count + 1,
        claim.max_retries + 1,
    )

    # --- the slow work, with no transaction held -------------------------
    try:
        storage = get_storage()
        with storage.open(claim.storage_key) as fh:
            result = extract_text_from_pdf(fh)
    except PERMANENT_ERRORS as exc:
        return _settle_permanent_failure(job_uuid, exc)
    except Exception as exc:
        # Includes SoftTimeLimitExceeded, storage I/O errors and database
        # blips - all worth another attempt.
        return _retry_or_dead_letter(self, job_uuid, exc, claim)

    # --- record success --------------------------------------------------
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is None:
            logger.error("Job %s vanished mid-flight", job_uuid)
            return _outcome(job_uuid, "IGNORED", reason="not_found")
        _set_status(job, JobStatus.DONE)
        job.result = result
        job.error_message = None
        job.next_retry_at = None
        job.completed_at = _now()

    logger.info(
        "Job %s DONE (%s pages, %s chars)",
        job_uuid,
        result["page_count"],
        result["char_count"],
    )
    return _outcome(
        job_uuid,
        JobStatus.DONE.value,
        page_count=result["page_count"],
        char_count=result["char_count"],
    )


@celery_app.task(name=TASK_REAP_STALE_JOBS)
def reap_stale_jobs() -> dict[str, Any]:
    """Rescue jobs orphaned by a worker that died without its message returning.

    `acks_late` covers the common case - Redis redelivers after the visibility
    timeout. This covers the rest: a message genuinely lost, or a broker that
    restarted without it. Without this sweep such jobs sit in PROCESSING
    forever with nothing left to move them.
    """
    cutoff = _now() - timedelta(seconds=settings.STALE_JOB_SECONDS)
    requeue: list[uuid.UUID] = []
    dead_lettered = 0

    with session_scope() as session:
        stale = session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.PROCESSING.value,
                Job.started_at < cutoff,
            )
            .order_by(Job.started_at)
            .limit(100)
            # skip_locked so overlapping sweeps never fight over the same row.
            .with_for_update(skip_locked=True)
        ).all()

        for job in stale:
            age = _age_seconds(job.started_at)
            if job.retry_count >= job.max_retries:
                _dead_letter(
                    job,
                    f"Orphaned in PROCESSING for {age:.0f}s; retry budget "
                    f"exhausted after {job.retry_count} attempt(s)",
                )
                dead_lettered += 1
                continue

            job.retry_count += 1
            _set_status(job, JobStatus.RETRYING)
            job.next_retry_at = _now()
            job.error_message = f"Reclaimed by the reaper after {age:.0f}s in PROCESSING"
            requeue.append(job.id)

    # Same rule as the upload route: commit first, enqueue second.
    for job_id in requeue:
        celery_app.send_task(TASK_EXTRACT_TEXT, args=[str(job_id)], queue="default")

    if requeue or dead_lettered:
        logger.warning(
            "Reaper requeued %s and dead-lettered %s stale job(s)",
            len(requeue),
            dead_lettered,
        )
    return {"requeued": len(requeue), "dead_lettered": dead_lettered}
