"""SQLAlchemy models and the job state machine.

Both the API and the worker import this module - it is the single definition
of what a job is and which status transitions are legal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import settings


class Base(DeclarativeBase):
    pass


class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    #: Transient failure; a retry is scheduled. See `next_retry_at`.
    RETRYING = "RETRYING"
    DONE = "DONE"
    #: Permanent failure - the input itself is bad, so retrying cannot help.
    FAILED = "FAILED"
    #: Retries were exhausted. Distinct from FAILED so "we gave up" is
    #: queryable separately from "this input was never going to work".
    DEAD_LETTER = "DEAD_LETTER"


class JobType(str, Enum):
    #: One upload = one job that walks the whole ingestion pipeline.
    DOCUMENT = "document"


class JobPriority(str, Enum):
    """How urgently a job should be picked up.

    Maps to a dedicated Celery queue rather than a numeric priority value.
    Celery's numeric priorities over Redis are implemented as multiple queue
    keys anyway, with fiddly semantics; naming the queues makes the routing
    explicit and lets workers be dedicated to a subset of them.
    """

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class JobStage(str, Enum):
    """Steps a document job walks through.

    A job is a *pipeline*, not a single step: one row advances through these
    stages rather than spawning a row per step. That keeps "is my document
    ready?" a single lookup, and lets every stage reuse the Phase 2 retry and
    dead-letter machinery unchanged.
    """

    #: Read the PDF's embedded text layer.
    EXTRACT_TEXT = "extract_text"
    #: Only entered when the text layer is empty or near-empty (a scan).
    OCR = "ocr"
    #: Chunk the text and write vectors.
    EMBED = "embed"


#: Statuses from which no further transition is possible.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD_LETTER}
)

#: Statuses a task is allowed to claim work from.
CLAIMABLE_STATUSES: frozenset[str] = frozenset(
    {JobStatus.PENDING, JobStatus.RETRYING}
)

#: The explicit state machine.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    JobStatus.PENDING: frozenset({JobStatus.PROCESSING, JobStatus.FAILED}),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.RETRYING,
            JobStatus.DEAD_LETTER,
            # A finished stage with more work ahead goes back to PENDING: the
            # next stage is queued and waiting for a worker, which is exactly
            # what PENDING means.
            JobStatus.PENDING,
        }
    ),
    JobStatus.RETRYING: frozenset(
        {JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.DEAD_LETTER}
    ),
    JobStatus.DONE: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.DEAD_LETTER: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class IllegalTransition(Exception):
    """Raised when code tries to move a job into a status it cannot reach."""


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'RETRYING', 'DONE', "
            "'FAILED', 'DEAD_LETTER')",
            name="ck_jobs_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count_non_negative"),
        CheckConstraint(
            "stage IN ('extract_text', 'ocr', 'embed')", name="ck_jobs_stage"
        ),
        CheckConstraint(
            "priority IN ('high', 'normal', 'low')", name="ck_jobs_priority"
        ),
        Index("ix_jobs_created_at", text("created_at DESC")),
        Index("ix_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Storage key, not an absolute path - resolved by core.storage. Keeping it
    # opaque is what lets Phase 6 swap local disk for S3 without a migration.
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=JobStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHA-256 of the uploaded bytes. Re-uploading identical content returns the
    # existing job instead of processing it twice. Enforced by a *partial*
    # unique index that excludes failed jobs, so a genuine retry after failure
    # is still allowed - see migration 0002.
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When the next attempt is due, while status is RETRYING.
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: The pipeline step currently running (or the one that failed).
    stage: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=JobStage.EXTRACT_TEXT.value,
        server_default=text("'extract_text'"),
    )
    #: Selects which queue the job's stages are routed to.
    priority: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=JobPriority.NORMAL.value,
        server_default=text("'normal'"),
    )
    #: Append-only record of every stage the job has completed: status,
    #: duration and a per-stage detail blob. This is what makes a mid-pipeline
    #: failure legible - the job status says it failed, `stages` says where.
    stages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.id} {self.job_type} {self.status}>"


class DocumentChunk(Base):
    """One embedded slice of a document's text.

    Chunks are deleted with their job (ON DELETE CASCADE) so a re-run cannot
    leave orphaned vectors behind.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("job_id", "chunk_index", name="uq_document_chunks_job_index"),
        Index("ix_document_chunks_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DocumentChunk job={self.job_id} #{self.chunk_index}>"
