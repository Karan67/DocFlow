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

    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3001", "http://127.0.0.1:3001"]


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
