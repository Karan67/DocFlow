"""Celery task definitions - the actual job logic.

A job is a *pipeline*, and every stage runs through one shared runner
(`_run_stage`): claim, do the work, settle. The stages differ only in the
function they hand to the runner, so retries, dead-lettering and orphan
reclaim behave identically at every step.

    extract_text ──(text layer found)──> embed
         │
         └────────(scanned)───────────> ocr ──> embed

Rules this module follows, all of which matter more than they look:

1. Database transactions are short and never wrap the slow work. A transaction
   held open across OCR - which takes seconds per page - would pin a
   connection for minutes.
2. Every exit path leaves the job in a terminal status. A job stuck in
   PROCESSING forever is worse than a job marked FAILED.
3. Failures are classified. Bad input fails permanently and immediately;
   everything else is transient and retried with exponential backoff.
4. The retry budget lives in Postgres, not in Celery task state, and resets at
   each stage so a slow start does not starve a later step.
5. Advancing a stage commits before it enqueues, exactly like the upload route.
"""

from __future__ import annotations

import logging
import random
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from celery.signals import worker_process_init
from sqlalchemy import delete, select

from core.config import settings
from core.database import session_scope
from core.models import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    DocumentChunk,
    IllegalTransition,
    Job,
    JobStage,
    JobStatus,
    can_transition,
)
from core.storage import StorageError, get_storage
from worker.celery_app import (
    TASK_EMBED,
    TASK_EXTRACT_TEXT,
    TASK_OCR,
    TASK_REAP_STALE_JOBS,
    celery_app,
    queue_for,
)
from worker.embed import embed_document, get_model
from worker.extract import ExtractionError, extract_text_from_pdf
from worker.ocr import needs_ocr, ocr_pdf

logger = logging.getLogger(__name__)


@worker_process_init.connect
def _preload_embedding_model(**_kwargs: Any) -> None:
    """Load the embedding model when a prefork child starts, not on first use.

    Measured cold-load is ~1 minute under CPU contention. Paying it at worker
    startup keeps it out of the first real job's latency, which is the whole
    point of having a warm worker pool.
    """
    try:
        get_model()
    except Exception:
        # A worker that cannot preload should still start; the embed stage will
        # fail loudly (and retry) rather than the whole worker refusing to boot.
        logger.exception("Could not preload the embedding model")


#: Failures that will never succeed on a retry - the input itself is the
#: problem. Everything else is treated as transient and retried with backoff.
PERMANENT_ERRORS = (ExtractionError, StorageError)

#: Which Celery task runs which pipeline stage.
STAGE_TASKS: dict[str, str] = {
    JobStage.EXTRACT_TEXT.value: TASK_EXTRACT_TEXT,
    JobStage.OCR.value: TASK_OCR,
    JobStage.EMBED.value: TASK_EMBED,
}

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


def _append_stage_entry(
    job: Job,
    stage: str,
    status: str,
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record a stage outcome. Reassigns the list so SQLAlchemy sees the change."""
    entry: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "at": _now().isoformat(),
    }
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if detail:
        entry["detail"] = detail
    job.stages = list(job.stages or []) + [entry]


def _dead_letter(job: Job, reason: str) -> None:
    """Terminal give-up: retries were available and are now exhausted."""
    _set_status(job, JobStatus.DEAD_LETTER)
    job.error_message = reason[:_MAX_ERROR_CHARS]
    job.completed_at = _now()
    job.next_retry_at = None


def _outcome(job_uuid: uuid.UUID, status: str, **extra: Any) -> dict[str, Any]:
    return {"job_id": str(job_uuid), "status": status, **extra}


@dataclass(frozen=True)
class StageOutcome:
    """What a stage produced and where the pipeline goes next."""

    #: Merged into `job.result`.
    result_patch: dict[str, Any]
    #: The next stage to run, or None when the pipeline is complete.
    next_stage: JobStage | None
    #: Recorded in the `stages` log for this step.
    log_detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _ClaimResult:
    claimed: bool
    #: Returned straight to Celery when the job could not be claimed.
    payload: dict[str, Any] | None = None
    job_id: uuid.UUID | None = None
    storage_key: str = ""
    file_name: str = ""
    retry_count: int = 0
    max_retries: int = 0
    #: The job's accumulated result so far - later stages read earlier output.
    result: dict[str, Any] = field(default_factory=dict)


def _claim_job(job_uuid: uuid.UUID, stage: JobStage) -> _ClaimResult:
    """Move a job into PROCESSING for `stage`, or explain why we are not."""
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

        if job.stage != stage.value:
            # A stale message for a step the pipeline has already moved past.
            logger.warning(
                "Job %s is at stage %s, not %s - skipping stale message",
                job_uuid,
                job.stage,
                stage.value,
            )
            return _ClaimResult(
                False, _outcome(job_uuid, job.status, reason="wrong_stage")
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
                    f"Orphaned in PROCESSING at stage {job.stage} for {age:.0f}s; "
                    f"retry budget exhausted after {job.retry_count} attempt(s)",
                )
                _append_stage_entry(
                    job, job.stage, JobStatus.DEAD_LETTER.value, {"reason": "orphaned"}
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
            job_id=job.id,
            storage_key=job.file_path,
            file_name=job.file_name,
            retry_count=job.retry_count,
            max_retries=job.max_retries,
            result=dict(job.result or {}),
        )


def _settle_permanent_failure(
    job_uuid: uuid.UUID, stage: JobStage, exc: Exception
) -> dict[str, Any]:
    """Bad input. No retry - it would fail identically every time."""
    detail = str(exc)
    logger.error("Job %s FAILED permanently at %s: %s", job_uuid, stage.value, detail)
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is not None and job.status not in TERMINAL_STATUSES:
            _set_status(job, JobStatus.FAILED)
            job.error_message = _truncate(detail, traceback.format_exc())
            job.completed_at = _now()
            job.next_retry_at = None
            _append_stage_entry(
                job, stage.value, JobStatus.FAILED.value, {"error": detail}
            )
    return _outcome(
        job_uuid, JobStatus.FAILED.value, stage=stage.value, error=detail, retryable=False
    )


def _retry_or_dead_letter(
    task: Any,
    job_uuid: uuid.UUID,
    stage: JobStage,
    exc: Exception,
    claim: _ClaimResult,
) -> dict[str, Any]:
    """Transient failure: schedule the next attempt, or give up if out of budget."""
    detail = f"{type(exc).__name__}: {exc}"
    message = _truncate(detail, traceback.format_exc())

    if claim.retry_count >= claim.max_retries:
        logger.error(
            "Job %s exhausted its %s retries at %s - dead-lettering: %s",
            job_uuid,
            claim.max_retries,
            stage.value,
            detail,
        )
        with session_scope() as session:
            job = session.get(Job, job_uuid, with_for_update=True)
            if job is not None and job.status not in TERMINAL_STATUSES:
                _dead_letter(job, message)
                _append_stage_entry(
                    job, stage.value, JobStatus.DEAD_LETTER.value, {"error": detail}
                )
        return _outcome(
            job_uuid,
            JobStatus.DEAD_LETTER.value,
            stage=stage.value,
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
        "Job %s failed transiently at %s (%s) - retry %s/%s in %ss",
        job_uuid,
        stage.value,
        detail,
        attempt,
        claim.max_retries,
        delay,
    )
    raise task.retry(exc=exc, countdown=delay)


def _complete_stage(
    job_uuid: uuid.UUID,
    stage: JobStage,
    outcome: StageOutcome,
    duration_ms: int,
    attempts: int,
) -> dict[str, Any]:
    """Record a finished stage and either advance the pipeline or finish it."""
    next_task: str | None = None
    next_queue: str = ""

    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is None:
            logger.error("Job %s vanished mid-flight", job_uuid)
            return _outcome(job_uuid, "IGNORED", reason="not_found")

        # retry_count resets when the stage advances, so without recording it
        # here a stage that retried twice before succeeding would leave no trace.
        _append_stage_entry(
            job,
            stage.value,
            JobStatus.DONE.value,
            {**outcome.log_detail, "attempts": attempts},
            duration_ms,
        )
        job.result = {**(job.result or {}), **outcome.result_patch}
        job.error_message = None
        job.next_retry_at = None

        if outcome.next_stage is None:
            _set_status(job, JobStatus.DONE)
            job.completed_at = _now()
        else:
            _set_status(job, JobStatus.PENDING)
            job.stage = outcome.next_stage.value
            # Each stage gets its own retry budget; a rocky extraction should
            # not leave the embedding step with nothing left.
            job.retry_count = 0
            job.started_at = None
            next_task = STAGE_TASKS[outcome.next_stage.value]
            next_stage_value = outcome.next_stage.value
            next_queue = queue_for(next_stage_value, job.priority)

    # Commit first, enqueue second - same rule as the upload route.
    if next_task is not None:
        celery_app.send_task(next_task, args=[str(job_uuid)], queue=next_queue)
        logger.info(
            "Job %s finished %s in %sms -> queued %s on %s",
            job_uuid,
            stage.value,
            duration_ms,
            next_stage_value,
            next_queue,
        )
        return _outcome(
            job_uuid,
            JobStatus.PENDING.value,
            stage=stage.value,
            next_stage=next_stage_value,
            queue=next_queue,
            duration_ms=duration_ms,
        )

    logger.info(
        "Job %s DONE - finished %s in %sms", job_uuid, stage.value, duration_ms
    )
    return _outcome(
        job_uuid, JobStatus.DONE.value, stage=stage.value, duration_ms=duration_ms
    )


def _run_stage(
    task: Any,
    job_id: str,
    stage: JobStage,
    work: Callable[[_ClaimResult], StageOutcome],
) -> dict[str, Any]:
    """Shared claim -> work -> settle machinery for every pipeline stage."""
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Received a task with an unusable job_id: %r", job_id)
        return {"job_id": job_id, "status": "IGNORED", "reason": "bad_job_id"}

    claim = _claim_job(job_uuid, stage)
    if not claim.claimed:
        return claim.payload  # type: ignore[return-value]

    logger.info(
        "Job %s PROCESSING stage=%s (%s, attempt %s/%s)",
        job_uuid,
        stage.value,
        claim.file_name,
        claim.retry_count + 1,
        claim.max_retries + 1,
    )

    # --- the slow work, with no transaction held -------------------------
    started = time.monotonic()
    try:
        outcome = work(claim)
    except PERMANENT_ERRORS as exc:
        return _settle_permanent_failure(job_uuid, stage, exc)
    except Exception as exc:
        # Includes SoftTimeLimitExceeded, storage I/O errors and database
        # blips - all worth another attempt.
        return _retry_or_dead_letter(task, job_uuid, stage, exc, claim)

    duration_ms = int((time.monotonic() - started) * 1000)
    return _complete_stage(
        job_uuid, stage, outcome, duration_ms, attempts=claim.retry_count + 1
    )


# --------------------------------------------------------------------------
# stage implementations
# --------------------------------------------------------------------------


def _do_extract_text(claim: _ClaimResult) -> StageOutcome:
    """Read the PDF's text layer, and decide whether OCR is needed."""
    storage = get_storage()
    with storage.open(claim.storage_key) as fh:
        extracted = extract_text_from_pdf(fh)

    page_count = extracted["page_count"]
    char_count = extracted["char_count"]

    if needs_ocr(page_count, char_count):
        # Route to OCR rather than returning an empty document. Keep the counts
        # so the finished job can show why OCR was used.
        logger.info(
            "Job %s has %s chars across %s pages - routing to OCR",
            claim.job_id,
            char_count,
            page_count,
        )
        return StageOutcome(
            result_patch={
                "page_count": page_count,
                "text_layer_chars": char_count,
                "needed_ocr": True,
            },
            next_stage=JobStage.OCR,
            log_detail={
                "page_count": page_count,
                "char_count": char_count,
                "decision": "no usable text layer -> ocr",
            },
        )

    return StageOutcome(
        result_patch={**extracted, "source": "text_layer", "needed_ocr": False},
        next_stage=JobStage.EMBED,
        log_detail={
            "page_count": page_count,
            "char_count": char_count,
            "decision": "text layer used",
        },
    )


def _do_ocr(claim: _ClaimResult) -> StageOutcome:
    """Render pages to images and recognise the text."""
    storage = get_storage()
    with storage.open(claim.storage_key) as fh:
        recognised = ocr_pdf(fh)

    return StageOutcome(
        result_patch=recognised,
        next_stage=JobStage.EMBED,
        log_detail={
            "page_count": recognised["page_count"],
            "char_count": recognised["char_count"],
            "pages_capped": recognised["pages_capped"],
        },
    )


def _do_embed(claim: _ClaimResult) -> StageOutcome:
    """Chunk the extracted text and write vectors."""
    text = (claim.result or {}).get("text") or ""

    if not text.strip():
        # A blank scan is a legitimate outcome, not a failure. Record it and
        # let the pipeline finish cleanly.
        logger.info("Job %s has no text to embed - skipping", claim.job_id)
        return StageOutcome(
            result_patch={"chunk_count": 0, "embedded": False},
            next_stage=None,
            log_detail={"skipped": "no text"},
        )

    embedded = embed_document(text)

    with session_scope() as session:
        # Delete first so a retry replaces its chunks instead of colliding with
        # the (job_id, chunk_index) unique constraint. This is what makes the
        # stage safe to re-run.
        session.execute(
            delete(DocumentChunk).where(DocumentChunk.job_id == claim.job_id)
        )
        session.add_all(
            [
                DocumentChunk(
                    job_id=claim.job_id,
                    chunk_index=index,
                    content=chunk,
                    embedding=vector,
                )
                for index, (chunk, vector) in enumerate(
                    zip(embedded["chunks"], embedded["vectors"])
                )
            ]
        )

    return StageOutcome(
        result_patch={
            "chunk_count": embedded["chunk_count"],
            "embedded": True,
            "embedding_model": embedded["model"],
            "embedding_dimensions": embedded["dimensions"],
        },
        next_stage=None,
        log_detail={
            "chunk_count": embedded["chunk_count"],
            "model": embedded["model"],
        },
    )


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

# Celery's own retry ceiling is disabled on every stage: the per-job
# max_retries column is the source of truth, so the budget survives worker
# restarts and is visible in the database rather than buried in task state.


@celery_app.task(name=TASK_EXTRACT_TEXT, bind=True, max_retries=None)
def extract_text_task(self, job_id: str) -> dict[str, Any]:
    """Stage 1: read the text layer, routing to OCR when there is not one."""
    return _run_stage(self, job_id, JobStage.EXTRACT_TEXT, _do_extract_text)


@celery_app.task(
    name=TASK_OCR,
    bind=True,
    max_retries=None,
    # OCR is the one genuinely slow stage; the global limits are sized for the
    # fast ones and would kill a long scan mid-page.
    soft_time_limit=settings.OCR_SOFT_TIME_LIMIT,
    time_limit=settings.OCR_HARD_TIME_LIMIT,
)
def ocr_task(self, job_id: str) -> dict[str, Any]:
    """Stage 2 (scanned documents only): recognise text from page images."""
    return _run_stage(self, job_id, JobStage.OCR, _do_ocr)


@celery_app.task(name=TASK_EMBED, bind=True, max_retries=None)
def embed_task(self, job_id: str) -> dict[str, Any]:
    """Stage 3: chunk the text and write vectors."""
    return _run_stage(self, job_id, JobStage.EMBED, _do_embed)


@celery_app.task(name=TASK_REAP_STALE_JOBS)
def reap_stale_jobs() -> dict[str, Any]:
    """Rescue jobs orphaned by a worker that died without its message returning.

    `acks_late` covers the common case - Redis redelivers after the visibility
    timeout. This covers the rest: a message genuinely lost, or a broker that
    restarted without it. Without this sweep such jobs sit in PROCESSING
    forever with nothing left to move them.
    """
    cutoff = _now() - timedelta(seconds=settings.STALE_JOB_SECONDS)
    pending_cutoff = _now() - timedelta(seconds=settings.ORPHANED_PENDING_SECONDS)
    requeue: list[tuple[uuid.UUID, str, str]] = []
    dead_lettered = 0
    revived = 0

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
                    f"Orphaned in PROCESSING at stage {job.stage} for {age:.0f}s; "
                    f"retry budget exhausted after {job.retry_count} attempt(s)",
                )
                _append_stage_entry(
                    job, job.stage, JobStatus.DEAD_LETTER.value, {"reason": "orphaned"}
                )
                dead_lettered += 1
                continue

            job.retry_count += 1
            _set_status(job, JobStatus.RETRYING)
            job.next_retry_at = _now()
            job.error_message = (
                f"Reclaimed by the reaper after {age:.0f}s in PROCESSING "
                f"at stage {job.stage}"
            )
            # Requeue the stage the job is actually on, not the entry point,
            # and onto the queue that stage belongs on.
            requeue.append(
                (job.id, STAGE_TASKS[job.stage], queue_for(job.stage, job.priority))
            )

        # Jobs that were committed but never enqueued: the API died between the
        # commit and the send_task. Nothing else will ever move them, because
        # the sweep above only looks at PROCESSING.
        #
        # Re-enqueuing is safe even if the message does exist after all - the
        # claim step skips anything already in flight or settled. The threshold
        # is deliberately long so an ordinary backlog is never disturbed.
        orphaned = session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.PENDING.value,
                Job.started_at.is_(None),
                Job.created_at < pending_cutoff,
            )
            .order_by(Job.created_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()

        for job in orphaned:
            age = _age_seconds(job.created_at)
            if job.retry_count >= job.max_retries:
                _dead_letter(
                    job,
                    f"Never enqueued; still PENDING after {age:.0f}s and the "
                    f"retry budget is exhausted",
                )
                _append_stage_entry(
                    job, job.stage, JobStatus.DEAD_LETTER.value, {"reason": "never_enqueued"}
                )
                dead_lettered += 1
                continue

            # Charge a retry so a job whose enqueue keeps failing cannot be
            # revived forever.
            job.retry_count += 1
            job.error_message = (
                f"Re-enqueued by the reaper after {age:.0f}s stuck in PENDING"
            )
            revived += 1
            requeue.append(
                (job.id, STAGE_TASKS[job.stage], queue_for(job.stage, job.priority))
            )

    # Same rule as the upload route: commit first, enqueue second.
    for job_id, task_name, queue_name in requeue:
        celery_app.send_task(task_name, args=[str(job_id)], queue=queue_name)

    if requeue or dead_lettered:
        logger.warning(
            "Reaper requeued %s (%s never enqueued) and dead-lettered %s job(s)",
            len(requeue),
            revived,
            dead_lettered,
        )
    return {
        "requeued": len(requeue),
        "revived_pending": revived,
        "dead_lettered": dead_lettered,
    }
