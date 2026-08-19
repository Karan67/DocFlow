"""Central configuration, shared by the API and the worker."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Defaults target the *host* ports so the app also runs outside Docker.
    # docker-compose overrides these with in-network hostnames.
    DATABASE_URL: str = "postgresql+psycopg2://docflow:docflow@localhost:5433/docflow"
    REDIS_URL: str = "redis://localhost:6380/0"

    # Compose overrides this with /data/uploads (the shared volume); the
    # relative default is for running the app directly on the host.
    UPLOAD_DIR: Path = Path("uploads")
    MAX_UPLOAD_BYTES: int = 25_000_000

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

    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3001", "http://127.0.0.1:3001"]


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
