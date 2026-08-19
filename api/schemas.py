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
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobDetail(JobSummary):
    """Full record for `GET /jobs/{job_id}`."""

    result: dict[str, Any] | None = None
    error_message: str | None = None


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


class HealthResponse(BaseModel):
    status: str
    database: str
    broker: str
