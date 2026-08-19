"""Synchronous SQLAlchemy engine/session, shared by API and worker.

Deliberately sync: Celery's prefork workers are synchronous, and FastAPI runs
plain `def` endpoints in a threadpool. One driver and one session factory means
no async/sync bridging between the two halves of the system.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    # Worker connections sit idle between tasks and get reaped by Postgres or
    # the network; pre_ping turns a stale-connection crash into a reconnect.
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # Keeps attributes readable after commit (e.g. job.id in the upload route).
    expire_on_commit=False,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker code. Commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Routes commit explicitly."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
