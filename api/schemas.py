"""Pydantic request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobSummary(BaseModel):
    """Row shape for `GET /jobs`. Omits `result` so listings stay small."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_name: str
    job_type: str
    status: str
    retry_count: int
    max_retries: int
    #: The pipeline step currently running, or the one that failed.
    stage: str
    #: Selects the queue this job's stages are routed to.
    priority: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: Populated while the job is RETRYING - when the next attempt is due.
    next_retry_at: datetime | None = None


class JobDetail(JobSummary):
    """Full record for `GET /jobs/{job_id}`."""

    result: dict[str, Any] | None = None
    error_message: str | None = None
    #: SHA-256 of the uploaded content; the deduplication key.
    idempotency_key: str | None = None
    #: Per-stage timeline: what ran, how long it took, and what it produced.
    #: A mid-pipeline failure shows up here as the stage that did not finish.
    stages: list[dict[str, Any]] = Field(default_factory=list)


class JobListResponse(BaseModel):
    items: list[JobSummary]
    total: int
    limit: int
    offset: int


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    job_type: str
    status_url: str = Field(
        description="Poll this endpoint for the job's current status."
    )
    deduplicated: bool = Field(
        default=False,
        description=(
            "True when identical content was already in the pipeline, so this "
            "upload returned the existing job instead of creating a new one. "
            "The response status is 200 rather than 202 in that case."
        ),
    )


class HealthResponse(BaseModel):
    status: str
    database: str
    broker: str
