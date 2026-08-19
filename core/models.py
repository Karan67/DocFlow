"""SQLAlchemy models and the job state machine.

Both the API and the worker import this module - it is the single definition
of what a job is and which status transitions are legal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    EXTRACT_TEXT = "extract_text"


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
