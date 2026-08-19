from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from api.main import app
from core.database import session_scope
from core.models import Job
from core.storage import get_storage
from worker.celery_app import celery_app

PAGE_ONE = "DocFlow phase one smoke test."
PAGE_TWO = "Second page of the sample document."
OTHER_PAGE = "A completely different document, with different bytes."


def _build_pdf(*lines: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=LETTER)
    for line in lines:
        pdf.drawString(72, 720, line)
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """A real two-page PDF with extractable text."""
    return _build_pdf(PAGE_ONE, PAGE_TWO)


@pytest.fixture
def other_pdf_bytes() -> bytes:
    """A different PDF, so it hashes to a different idempotency key."""
    return _build_pdf(OTHER_PAGE)


@pytest.fixture
def client(monkeypatch):
    """TestClient with the broker stubbed out; records what would be enqueued."""
    sent: list[tuple] = []

    def fake_send_task(name, args=None, **kwargs):
        sent.append((name, args, kwargs))
        return SimpleNamespace(id=str(uuid.uuid4()))

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    with TestClient(app) as test_client:
        test_client.sent = sent  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture
def cleanup_jobs():
    """Job ids to delete (with their stored files) when the test finishes."""
    created: list[uuid.UUID] = []
    yield created
    storage = get_storage()
    with session_scope() as session:
        for job_id in created:
            job = session.get(Job, job_id)
            if job is not None:
                storage.delete(job.file_path)
                session.delete(job)


@pytest.fixture
def upload(client, cleanup_jobs):
    """POST a PDF to the upload endpoint and register it for cleanup."""

    def _upload(pdf_bytes: bytes, filename: str = "sample.pdf"):
        response = client.post(
            "/jobs/upload",
            files={"file": (filename, pdf_bytes, "application/pdf")},
        )
        if response.status_code in (200, 202):
            job_id = uuid.UUID(response.json()["job_id"])
            if job_id not in cleanup_jobs:
                cleanup_jobs.append(job_id)
        return response

    return _upload


@pytest.fixture
def force_job_state():
    """Push a job directly into a given state, bypassing the state machine."""

    def _force(job_id: uuid.UUID, **fields: Any) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None, f"no job {job_id}"
            for name, value in fields.items():
                setattr(job, name, value)

    return _force


@pytest.fixture
def read_job():
    """Fetch a fresh copy of a job's fields as a plain dict."""

    def _read(job_id: uuid.UUID) -> dict[str, Any]:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None, f"no job {job_id}"
            return {
                "status": job.status,
                "retry_count": job.retry_count,
                "max_retries": job.max_retries,
                "result": job.result,
                "error_message": job.error_message,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "next_retry_at": job.next_retry_at,
                "idempotency_key": job.idempotency_key,
            }

    return _read
