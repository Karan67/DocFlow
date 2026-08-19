"""Job endpoints. The API accepts work and reports status - it never does the work."""

from __future__ import annotations

import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.schemas import JobCreatedResponse, JobDetail, JobListResponse, JobSummary
from core.config import settings
from core.database import get_db
from api.limiter import limiter
from core.models import Job, JobPriority, JobStage, JobStatus, JobType
from core.storage import FileTooLarge, get_storage
from worker.celery_app import TASK_EXTRACT_TEXT, celery_app, queue_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

ALLOWED_SUFFIXES = {".pdf"}

#: Jobs in these statuses no longer reserve their idempotency key, so the same
#: file may be uploaded again to get a fresh attempt.
_DEDUPE_EXEMPT = [JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value]


def _find_active_duplicate(db: Session, idempotency_key: str) -> Job | None:
    """The existing job holding this content hash, if it has not failed."""
    return db.scalars(
        select(Job)
        .where(
            Job.idempotency_key == idempotency_key,
            Job.status.notin_(_DEDUPE_EXEMPT),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    ).first()


def _resolve_priority(requested: JobPriority | None, size_bytes: int) -> str:
    """Explicit priority wins; otherwise large uploads step aside."""
    if requested is not None:
        return requested.value
    if size_bytes > settings.LARGE_FILE_BYTES:
        # One 40MB scan should not make a queue of one-page invoices wait.
        return JobPriority.LOW.value
    return JobPriority.NORMAL.value


def _deduplicated_response(response: Response, existing: Job) -> JobCreatedResponse:
    # 200 rather than 202: nothing new was accepted for processing.
    response.status_code = status.HTTP_200_OK
    return JobCreatedResponse(
        job_id=existing.id,
        status=existing.status,
        job_type=existing.job_type,
        status_url=f"/jobs/{existing.id}",
        deduplicated=True,
    )


@router.post(
    "/upload",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and queue it for processing",
)
@limiter.limit(settings.RATE_LIMIT_UPLOAD)
def upload_job(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    priority: JobPriority | None = Form(
        None, description="high, normal or low. Defaults by file size."
    ),
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

    # Idempotency: the content hash identifies the work. Re-uploading a file
    # that is queued, running or already done returns the original job rather
    # than processing the same bytes twice.
    existing = _find_active_duplicate(db, stored.sha256)
    if existing is not None:
        storage.delete(stored.key)
        logger.info(
            "Upload of %s deduplicated onto existing job %s", file_name, existing.id
        )
        return _deduplicated_response(response, existing)

    resolved_priority = _resolve_priority(priority, stored.size)
    job = Job(
        file_name=file_name,
        file_path=stored.key,
        job_type=JobType.DOCUMENT.value,
        status=JobStatus.PENDING.value,
        priority=resolved_priority,
        max_retries=settings.DEFAULT_MAX_RETRIES,
        idempotency_key=stored.sha256,
    )

    # Commit BEFORE enqueuing. A worker can pick the task up within
    # milliseconds; if the row is not committed yet it will look up a job that
    # does not exist.
    try:
        db.add(job)
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent upload of identical content. The
        # partial unique index is the real guard - the lookup above is only the
        # fast path - so fall back to returning whichever job won.
        db.rollback()
        storage.delete(stored.key)
        winner = _find_active_duplicate(db, stored.sha256)
        if winner is None:
            logger.exception("Integrity error with no surviving job for %s", file_name)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not create the job"
            )
        logger.info("Upload of %s lost a dedupe race to job %s", file_name, winner.id)
        return _deduplicated_response(response, winner)
    except Exception:
        db.rollback()
        storage.delete(stored.key)
        logger.exception("Could not create job row for %s", file_name)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not create the job"
        )

    queue_name = queue_for(JobStage.EXTRACT_TEXT.value, resolved_priority)
    try:
        celery_app.send_task(TASK_EXTRACT_TEXT, args=[str(job.id)], queue=queue_name)
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

    logger.info(
        "Queued job %s for %s (%s bytes) on %s",
        job.id,
        file_name,
        stored.size,
        queue_name,
    )
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
