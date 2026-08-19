"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.limiter import limiter
from api.routes import router as jobs_router
from api.stats import router as stats_router
from api.schemas import HealthResponse
from core.config import configure_logging, settings
from core.database import engine

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="DocFlow",
    version="0.1.0",
    description=(
        "Distributed document processing pipeline. The API accepts uploads and "
        "reports job status; Celery workers do the actual processing."
    ),
)

# slowapi reads the limiter off app.state and needs a handler for its own
# exception type, otherwise a tripped limit surfaces as a 500.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs_router)
app.include_router(stats_router)


def _check_database() -> str:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        return f"error: {type(exc).__name__}"


def _check_broker() -> str:
    client = None
    try:
        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        client.ping()
        return "ok"
    except Exception as exc:
        logger.warning("Broker health check failed: %s", exc)
        return f"error: {type(exc).__name__}"
    finally:
        if client is not None:
            client.close()


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> JSONResponse:
    """Readiness probe. Compose gates the worker on this returning 200."""
    database = _check_database()
    broker = _check_broker()
    healthy = database == "ok" and broker == "ok"
    payload = HealthResponse(
        status="ok" if healthy else "degraded", database=database, broker=broker
    )
    return JSONResponse(
        status_code=200 if healthy else 503, content=payload.model_dump()
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {"service": "docflow", "docs": "/docs", "health": "/health"}
