"""Job endpoints. The API accepts work and reports status - it never does the work."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas import JobCreatedResponse, JobDetail, JobListResponse, JobSummary
from core.config import settings
from core.database import get_db
from core.models import Job, JobStatus, JobType
from core.storage import FileTooLarge, get_storage
from worker.celery_app import TASK_EXTRACT_TEXT, celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

ALLOWED_SUFFIXES = {".pdf"}


@router.post(
    "/upload",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and queue it for processing",
)
def upload_job(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> JobCreatedResponse:
    file_name = (file.filename or "").strip()
    if not file_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A filename is required")

    suffix = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Phase 1 accepts {sorted(ALLOWED_SUFFIXES)} only, got {suffix or 'no extension'}",
        )

    storage = get_storage()
    try:
        stored = storage.save(file.file, file_name)
    except FileTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    except OSError as exc:
        logger.exception("Could not persist upload %s", file_name)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not store the uploaded file"
        ) from exc

    if stored.size == 0:
        storage.delete(stored.key)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")

    job = Job(
        file_name=file_name,
        file_path=stored.key,
        job_type=JobType.EXTRACT_TEXT.value,
        status=JobStatus.PENDING.value,
        max_retries=settings.DEFAULT_MAX_RETRIES,
    )

    # Commit BEFORE enqueuing. A worker can pick the task up within
    # milliseconds; if the row is not committed yet it will look up a job that
    # does not exist.
    try:
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(stored.key)
        logger.exception("Could not create job row for %s", file_name)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not create the job"
        )

    try:
        celery_app.send_task(TASK_EXTRACT_TEXT, args=[str(job.id)], queue="default")
    except Exception as exc:
        # The broker is unreachable. Settle the job rather than leaving it
        # PENDING forever with nothing to pick it up.
        logger.exception("Could not enqueue job %s", job.id)
        job.status = JobStatus.FAILED.value
        job.error_message = f"Could not enqueue task: {type(exc).__name__}: {exc}"
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Job was recorded but could not be queued; the broker is unavailable",
        ) from exc

    logger.info("Queued job %s for %s (%s bytes)", job.id, file_name, stored.size)
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        job_type=job.job_type,
        status_url=f"/jobs/{job.id}",
    )


@router.get("", response_model=JobListResponse, summary="List recent jobs")
def list_jobs(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    job_status: JobStatus | None = Query(None, alias="status"),
) -> JobListResponse:
    filters = []
    if job_status is not None:
        filters.append(Job.status == job_status.value)

    total = db.scalar(select(func.count()).select_from(Job).where(*filters)) or 0
    rows = db.scalars(
        select(Job)
        .where(*filters)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return JobListResponse(
        items=[JobSummary.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=JobDetail, summary="Get one job's status")
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobDetail:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job with id {job_id}")
    return JobDetail.model_validate(job)
