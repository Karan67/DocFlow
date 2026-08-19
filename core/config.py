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

    DEFAULT_MAX_RETRIES: int = 3
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3001", "http://127.0.0.1:3001"]


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
