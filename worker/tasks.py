"""Celery task definitions - the actual job logic.

Two rules this module follows, both of which matter more than they look:

1. Database transactions are short and never wrap the slow work. A transaction
   held open across file I/O pins a connection and blocks other writers.
2. Every exit path leaves the job in a terminal status. A job stuck in
   PROCESSING forever is worse than a job marked FAILED.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from core.database import session_scope
from core.models import IllegalTransition, Job, JobStatus, can_transition
from core.storage import get_storage
from worker.celery_app import TASK_EXTRACT_TEXT, celery_app
from worker.extract import ExtractionError, extract_text_from_pdf

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_status(job: Job, target: JobStatus) -> None:
    if not can_transition(job.status, target.value):
        raise IllegalTransition(
            f"Job {job.id} cannot move from {job.status} to {target.value}"
        )
    job.status = target.value


@celery_app.task(name=TASK_EXTRACT_TEXT, bind=True)
def extract_text_task(self, job_id: str) -> dict[str, Any]:
    """PENDING -> PROCESSING -> DONE | FAILED for a single extract_text job."""
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Received a task with an unusable job_id: %r", job_id)
        return {"job_id": job_id, "status": "IGNORED", "reason": "bad_job_id"}

    # --- 1. Claim the job -------------------------------------------------
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)

        if job is None:
            # Almost always means the task was enqueued before the row was
            # committed. The upload route commits first precisely to avoid it.
            logger.error("No job row for %s - dropping task", job_uuid)
            return {"job_id": job_id, "status": "IGNORED", "reason": "not_found"}

        if job.status != JobStatus.PENDING.value:
            # Duplicate delivery, or a retry of something already settled.
            logger.warning(
                "Job %s is already %s - skipping", job_uuid, job.status
            )
            return {"job_id": job_id, "status": job.status, "reason": "already_handled"}

        _set_status(job, JobStatus.PROCESSING)
        job.started_at = _now()
        storage_key = job.file_path
        file_name = job.file_name

    logger.info("Job %s PROCESSING (%s)", job_uuid, file_name)

    # --- 2. Do the slow work, with no transaction held --------------------
    try:
        storage = get_storage()
        with storage.open(storage_key) as fh:
            result = extract_text_from_pdf(fh)
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, ExtractionError)
            else f"{type(exc).__name__}: {exc}"
        )
        logger.exception("Job %s FAILED", job_uuid)
        with session_scope() as session:
            job = session.get(Job, job_uuid, with_for_update=True)
            if job is not None and job.status not in {
                JobStatus.DONE.value,
                JobStatus.FAILED.value,
            }:
                _set_status(job, JobStatus.FAILED)
                job.error_message = f"{detail}\n\n{traceback.format_exc()}"[:8000]
                job.completed_at = _now()
        # Phase 2 turns this into a retry with backoff. For now, surfacing the
        # failure to Celery/Flower is enough.
        return {"job_id": job_id, "status": JobStatus.FAILED.value, "error": detail}

    # --- 3. Record success ------------------------------------------------
    with session_scope() as session:
        job = session.get(Job, job_uuid, with_for_update=True)
        if job is None:
            logger.error("Job %s vanished mid-flight", job_uuid)
            return {"job_id": job_id, "status": "IGNORED", "reason": "not_found"}
        _set_status(job, JobStatus.DONE)
        job.result = result
        job.error_message = None
        job.completed_at = _now()

    logger.info(
        "Job %s DONE (%s pages, %s chars)",
        job_uuid,
        result["page_count"],
        result["char_count"],
    )
    return {
        "job_id": job_id,
        "status": JobStatus.DONE.value,
        "page_count": result["page_count"],
        "char_count": result["char_count"],
    }
