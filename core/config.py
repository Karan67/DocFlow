"""Central configuration, shared by the API and the worker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Defaults target the *host* ports so the app also runs outside Docker.
    # docker-compose overrides these with in-network hostnames.
    DATABASE_URL: str = "postgresql+psycopg2://docflow:docflow@localhost:5433/docflow"
    REDIS_URL: str = "redis://localhost:6380/0"

    # --- storage ---
    #: "local" (a shared Docker volume) or "s3". The rest of the codebase only
    #: ever handles an opaque key, so switching needs no migration.
    STORAGE_BACKEND: Literal["local", "s3"] = "local"

    # Compose overrides this with /data/uploads (the shared volume); the
    # relative default is for running the app directly on the host.
    UPLOAD_DIR: Path = Path("uploads")
    MAX_UPLOAD_BYTES: int = 25_000_000

    S3_BUCKET: str = "docflow-uploads"
    S3_PREFIX: str = "uploads/"
    #: Set for an S3-compatible endpoint such as MinIO. Leave unset for real
    #: AWS, where boto3 resolves the endpoint from the region.
    S3_ENDPOINT_URL: str | None = None
    AWS_REGION: str = "us-east-1"
    # Credentials deliberately absent: boto3 resolves them from environment
    # variables in development and an instance role in production. Putting
    # access keys in application config is how they end up in git.

    # Extracted text is stored in a JSONB column; cap it so one giant PDF
    # cannot bloat the jobs table.
    MAX_RESULT_TEXT_CHARS: int = 50_000

    # --- Phase 2: reliability ---
    DEFAULT_MAX_RETRIES: int = 3

    #: Exponential backoff. Attempt N waits BASE * 2**N seconds, capped at MAX,
    #: then has jitter applied so a burst of simultaneous failures does not
    #: retry in lockstep (the thundering-herd problem).
    RETRY_BACKOFF_BASE: int = 4
    RETRY_BACKOFF_MAX: int = 600
    RETRY_JITTER: bool = True

    #: A task exceeding the soft limit gets an exception it can clean up from;
    #: the hard limit kills the worker process. Without these a hung task holds
    #: a concurrency slot forever.
    TASK_SOFT_TIME_LIMIT: int = 300
    TASK_HARD_TIME_LIMIT: int = 360

    #: A job PROCESSING for longer than this is presumed orphaned by a dead
    #: worker and gets reclaimed. Must stay comfortably above the hard time
    #: limit, or the reaper will steal jobs that are legitimately still running.
    STALE_JOB_SECONDS: int = 900
    #: How often the reaper sweeps for orphaned jobs.
    REAPER_INTERVAL_SECONDS: int = 300

    #: A job still PENDING this long after creation was probably never enqueued
    #: - the API committed the row and then died before `send_task`. Set well
    #: above STALE_JOB_SECONDS so an ordinary backlog is never touched.
    ORPHANED_PENDING_SECONDS: int = 1800

    # --- Phase 3: processing pipeline ---
    #: A PDF whose text layer yields fewer than this many characters per page
    #: is treated as scanned and routed to OCR instead.
    OCR_MIN_CHARS_PER_PAGE: int = 40
    OCR_LANGUAGE: str = "eng"
    OCR_DPI: int = 200
    #: OCR is orders of magnitude slower than reading a text layer. Cap the
    #: page count so one huge scan cannot blow through TASK_HARD_TIME_LIMIT.
    OCR_MAX_PAGES: int = 20
    #: OCR gets its own, much longer limits. Measured at roughly 4s per page
    #: warm, so OCR_MAX_PAGES pages need far more headroom than the global
    #: default - which stays tight for the fast stages. STALE_JOB_SECONDS must
    #: remain above OCR_HARD_TIME_LIMIT or the reaper would steal running work.
    OCR_SOFT_TIME_LIMIT: int = 600
    OCR_HARD_TIME_LIMIT: int = 660

    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    #: Must match the model. Changing one without the other fails at insert
    #: time against the vector(N) column.
    EMBEDDING_DIM: int = 384
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 150
    EMBEDDING_BATCH_SIZE: int = 32

    # --- Phase 4: scale ---
    #: Uploads above this size default to the low-priority queue so one large
    #: document cannot make a queue of small ones wait. An explicit priority on
    #: the request always wins.
    LARGE_FILE_BYTES: int = 5_000_000

    #: Requests per window, per client IP, on the upload endpoint.
    RATE_LIMIT_UPLOAD: str = "30/minute"
    RATE_LIMIT_ENABLED: bool = True

    #: How many trusted reverse proxies sit in front of the API. 0 means the
    #: socket address is the client. Anything above 0 reads that many entries
    #: back from the right of X-Forwarded-For - a header the client controls,
    #: so trusting it without knowing the hop count lets anyone spoof their IP
    #: and bypass the rate limit entirely.
    TRUSTED_PROXY_COUNT: int = 0

    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3001", "http://127.0.0.1:3001"]


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
